import os
import json
import random
import asyncio
import time
import re
import aiohttp
import difflib
import zipfile
import io
import datetime
import gc
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from PIL import Image as PILImage

# 阴历库检测
HAS_LUNAR = False
try:
    from lunar_python import Lunar, Solar
    HAS_LUNAR = True
except ImportError:
    pass

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print("DEBUG: MemeMaster Pro (V10 救赎版) 正在启动...")

@register("vv_meme_master", "MemeMaster", "救赎版V10", "3.6.8")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.memory_file = os.path.join(self.base_dir, "memory.txt") 
        self.buffer_file = os.path.join(self.base_dir, "buffer.json") 
        
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.running = True 
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123" 
            self.save_config()

        self.data = self.load_data()
        self.img_hashes = {}
        self.debounce_tasks = {}
        self.msg_buffers = {}
        
        self.chat_history_buffer = self.load_buffer_from_disk()
        self.last_active_time = time.time()
        self.current_summary = self.load_memory()
        
        self.left_pairs = {'“': '”', '《': '》', '（': '）', '(': ')', '[': ']', '{': '}'}
        self.right_pairs = {v: k for k, v in self.left_pairs.items()}

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            loop.create_task(self._init_image_hashes())
            loop.create_task(self._lonely_watcher())
        except Exception as e:
            print(f"ERROR: [Meme] 任务启动失败: {e}")

    def __del__(self):
        self.running = False 

    # ==========================
    # 消息处理
    # ==========================
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def handle_group_msg(self, event: AstrMessageEvent):
        await self._core_logic(event)

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_msg(self, event: AstrMessageEvent):
        await self._core_logic(event)

    async def _core_logic(self, event: AstrMessageEvent):
        try:
            if str(event.message_obj.sender.user_id) == str(self.context.get_current_provider_bot().self_id): return
        except: pass

        msg_str = (event.message_str or "").strip()
        img_url = self._get_img_url(event)
        uid = event.unified_msg_origin

        if not msg_str and not img_url: return

        self.last_active_time = time.time()
        self.last_session_id = event.session_id
        self.last_uid = uid

        if msg_str: 
            self.chat_history_buffer.append(f"User: {msg_str}")
            self.save_buffer_to_disk()

        if img_url and not msg_str.startswith("/"):
            if time.time() - getattr(self, "last_auto_save_time", 0) > self.local_config.get("auto_save_cooldown", 60):
                asyncio.create_task(self.ai_evaluate_image(img_url, msg_str))

        if msg_str.startswith(("/", "！", "!")):
            # 指令直接放行，不走防抖
            if uid in self.debounce_tasks: self.debounce_tasks[uid].cancel(); await self._execute_buffer(uid, event)
            return

        debounce_time = self.local_config.get("debounce_time", 5.0)
        
        # [重点] 拦截原生回复，但因为我们后续会带 session_id 请求，
        # 所以 AstrBot 内部会处理历史记录上下文（虽然 Dashboard 还是看不到这条记录，但 AI 脑子里会有）
        event.stop_event() 
        
        if debounce_time <= 0: 
            await self._execute_buffer(uid, event)
            return

        if uid not in self.msg_buffers: self.msg_buffers[uid] = {'text': [], 'imgs': [], 'event': event}
        self.msg_buffers[uid]['event'] = event 
        if msg_str: self.msg_buffers[uid]['text'].append(msg_str)
        if img_url: self.msg_buffers[uid]['imgs'].append(img_url)

        print(f"🕒 [Meme] 收到消息，进入防抖倒计时: {debounce_time}秒")

        if uid in self.debounce_tasks: self.debounce_tasks[uid].cancel()
        self.debounce_tasks[uid] = asyncio.create_task(self._debounce_waiter(uid, debounce_time))

    async def _debounce_waiter(self, uid, duration):
        try:
            await asyncio.sleep(duration)
            await self._execute_buffer(uid)
        except asyncio.CancelledError: pass

    async def _execute_buffer(self, uid, force_event=None):
        if uid not in self.msg_buffers and not force_event: return
        
        print(f"🚀 [Meme] 防抖结束，开始构建回复 (UID: {uid})")

        if uid in self.msg_buffers:
            data = self.msg_buffers.pop(uid)
            event = force_event or data['event']
            texts = data['text']
            imgs = data['imgs']
        else:
            event = force_event
            texts = [(event.message_str or "").strip()]
            imgs = []
            img = self._get_img_url(event)
            if img: imgs.append(img)
        
        asyncio.create_task(self.check_and_summarize())

        # ==============================
        # [V10 核心逻辑] 不覆盖 System Prompt，而是把信息附着在用户消息里
        # ==============================
        
        time_info = self.get_time_str()
        memory_block = f"长期记忆:{self.current_summary}" if self.current_summary else ""
        
        hint_msg = ""
        # 只有在有图的时候才给提示，防止干扰正常聊天
        if random.randint(1, 100) <= self.local_config.get("reply_prob", 50):
            all_tags = [v.get("tags", "").split(":")[0].strip() for v in self.data.values()]
            if all_tags:
                hints = random.sample(all_tags, min(15, len(all_tags)))
                hint_str = " ".join([f"<MEME:{h}>" for h in hints])
                hint_msg = f"\n(Hidden Hint: 可用表情包 {hint_str}，若合适请用 <MEME:名称> 格式)"

        # 拼接用户消息
        user_message_content = " ".join(texts)
        
        # 将环境信息、记忆、Hint 作为“旁白”附着在后面
        # 这样 AstrBot 会把这段话当做用户发来的，但 AI 会看到这些信息
        # 最重要的是：Session ID 保持不变，所以 AstrBot 原本的人设 Context 会生效！
        augmented_text = f"{user_message_content}\n\n[System Info: {time_info}. {memory_block}{hint_msg}]"
        
        provider = self.context.get_using_provider()
        if provider:
            try:
                # [关键] 必须传入 session_id，这样 AstrBot 才会去查它自己的数据库获取人设和历史
                # 我们传入的是 prompt=augmented_text，这会被视为当次的用户输入
                resp = await provider.text_chat(prompt=augmented_text, session_id=event.session_id, image_urls=imgs)
                reply = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                if reply: 
                    self.chat_history_buffer.append(f"AI: {reply}")
                    self.save_buffer_to_disk()
                    await self.process_and_send(event, reply)
            except Exception as e: print(f"LLM请求失败: {e}")

    async def check_and_summarize(self):
        if len(self.chat_history_buffer) < 50: return
        current_batch = list(self.chat_history_buffer)
        history_text = "\n".join(current_batch)
        
        print(f"📖 [Meme] 触发记忆总结...")
        provider = self.context.get_using_provider()
        if not provider: return

        now_str = self.get_time_str()
        prompt = f"""请将以下近期对话总结为一段简短的日记/记忆片段（200字内），保留关键信息和你的感受。
对话内容：
{history_text}"""

        try:
            # 总结不需要 session_id，因为是独立任务
            resp = await provider.text_chat(prompt=prompt, session_id=None)
            summary = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if summary:
                def write_task():
                    with open(self.memory_file, "a", encoding="utf-8") as f: 
                        f.write(f"\n\n--- {now_str} ---\n{summary}")
                await asyncio.get_running_loop().run_in_executor(self.executor, write_task)
                
                self.current_summary = self.load_memory()
                self.chat_history_buffer = self.chat_history_buffer[len(current_batch):]
                self.save_buffer_to_disk()
                print(f"✅ [Meme] 记忆总结成功")
        except Exception as e:
            if len(self.chat_history_buffer) > 100:
                self.chat_history_buffer = self.chat_history_buffer[-50:]
                self.save_buffer_to_disk()

    async def process_and_send(self, event, text, target_uid=None):
        text = text.replace("**", "").replace("### ", "")
        print(f"📤 [Meme] AI回复: {text[:30]}...")
        try:
            pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
            parts = re.split(pattern, text)
            mixed_chain = []
            
            for part in parts:
                tag = None
                if part.startswith("<MEME:"): tag = part[6:-1].strip()
                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                
                if tag:
                    path = self.find_best_match(tag)
                    if path: mixed_chain.append(Image.fromFileSystem(path))
                elif part:
                    mixed_chain.append(Plain(part))
            
            segments = self.smart_split(mixed_chain)
            uid = target_uid or event.unified_msg_origin
            
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_c = "".join([c.text for c in seg if isinstance(c, Plain)])
                mc = MessageChain(seg)
                await self.context.send_message(uid, mc)
                if i < len(segments) - 1:
                    await asyncio.sleep(delay_base + len(txt_c) * delay_factor)
        except Exception as e:
            print(f"发送出错: {e}")

    def smart_split(self, chain):
        segs = []; buf = []
        stack = [] 
        for c in chain:
            if isinstance(c, Image): 
                if buf: segs.append(buf[:]); buf.clear()
                segs.append([c]); continue
            if isinstance(c, Plain):
                txt = c.text; idx = 0; chunk = ""
                while idx < len(txt):
                    char = txt[idx]
                    if char in self.left_pairs: stack.append(self.left_pairs[char])
                    elif char in self.right_pairs and stack and stack[-1] == char: stack.pop()
                    chunk += char
                    if not stack and char in "\n。？！?!":
                        if idx + 1 < len(txt) and txt[idx+1] in "\n。？！?!": pass 
                        else:
                            if chunk.strip(): buf.append(Plain(chunk))
                            if buf: segs.append(buf[:]); buf.clear()
                            chunk = ""
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        if buf: segs.append(buf)
        return segs

    def compress_image_sync(self, image_data: bytes) -> tuple[bytes, str]:
        try:
            img = PILImage.open(io.BytesIO(image_data))
            if getattr(img, 'is_animated', False) or img.format == 'GIF': return image_data, ".gif"
            
            max_size = 350
            w, h = img.size
            if w > max_size or h > max_size:
                ratio = max_size / max(w, h)
                img = img.resize((int(w*ratio), int(h*ratio)), PILImage.Resampling.LANCZOS)
            
            buffer = io.BytesIO()
            if img.mode != "RGB": img = img.convert("RGB")
            img.save(buffer, format="JPEG", quality=75, optimize=True)
            return buffer.getvalue(), ".jpg"
        except: return image_data, ".jpg"

    async def ai_evaluate_image(self, img_url, context_text=""):
        try:
            self.last_auto_save_time = time.time()
            img_data = await self.download_image(img_url)
            if not img_data or len(img_data) > 5 * 1024 * 1024: return

            loop = asyncio.get_running_loop()
            current_hash = await loop.run_in_executor(self.executor, self.calc_dhash, img_data)
            if current_hash:
                for _, eh in self.img_hashes.items():
                    try:
                        if bin(int(current_hash, 16) ^ int(eh, 16)).count('1') <= 5: return
                    except: continue

            provider = self.context.get_using_provider()
            if not provider: return
            # 鉴图是独立任务，这里需要明确的Prompt，不依赖人设
            default_prompt = "这是二次元/Meme环境。配文:{context_text}。若适合存为表情包，请回复: YES\n<MEME:名称>: 说明"
            prompt = self.local_config.get("ai_prompt", default_prompt).replace("{context_text}", context_text)
            
            resp = await provider.text_chat(prompt=prompt, session_id=None, image_urls=[img_url])
            content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            if "YES" in content:
                match = re.search(r"<MEME:(.*?)>[:：]?(.*)", content)
                if match:
                    full_tag = f"{match.group(1).strip()}: {match.group(2).strip()}"
                    print(f"🖤 [自动进货] {full_tag}")
                    comp_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, img_data)
                    fn = f"{int(time.time())}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp_data)
                    self.data[fn] = {"tags": full_tag, "source": "auto"}
                    if current_hash: self.img_hashes[fn] = current_hash 
                    self.save_data()
        except: pass

    def calc_dhash(self, image_data: bytes) -> str:
        try:
            img = PILImage.open(io.BytesIO(image_data))
            if getattr(img, 'is_animated', False): img.seek(0)
            img = img.resize((9, 8), PILImage.Resampling.LANCZOS).convert('L')
            pixels = list(img.getdata()); diff = []
            for row in range(8):
                for col in range(8): diff.append(pixels[row*9+col] > pixels[row*9+col+1])
            val = 0
            for i, v in enumerate(diff): 
                if v: val += 2**i
            return hex(val)[2:]
        except: return None
    
    async def download_image(self, url):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.get(url) as r: return await r.read() if r.status==200 else None
        except: return None

    def find_best_match(self, query):
        best, score = None, 0
        for f, i in self.data.items():
            t_key = i.get("tags", "").split(":")[0].strip()
            if query == t_key: return os.path.join(self.img_dir, f)
            if query in i.get("tags", ""): return os.path.join(self.img_dir, f)
            s = difflib.SequenceMatcher(None, query, t_key).ratio()
            if s > score: score = s; best = f
        return os.path.join(self.img_dir, best) if score > 0.4 else None

    def get_time_str(self):
        now = datetime.datetime.now()
        solar_str = f"{now.strftime('%Y-%m-%d %H:%M')}"
        lunar_str = ""
        if HAS_LUNAR:
            try:
                lunar = Solar.fromYmdHms(now.year, now.month, now.day, now.hour, now.minute, now.second).getLunar()
                lunar_str = f" 农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}"
            except: pass
        return f"[时间: {solar_str}{lunar_str}]"

    def _get_img_url(self, e):
        for c in e.message_obj.message:
            if isinstance(c, Image): return c.url
        return None
    def load_config(self): return {**{"web_port":5000,"debounce_time":5.0,"reply_prob":50,"proactive_interval":0,"summary_threshold":50}, **(json.load(open(self.config_file)) if os.path.exists(self.config_file) else {})}
    def save_config(self): json.dump(self.local_config, open(self.config_file,"w"), indent=2)
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)
    def load_buffer_from_disk(self):
        try: return json.load(open(self.buffer_file, "r"))
        except: return []
    def save_buffer_to_disk(self):
        try: json.dump(self.chat_history_buffer, open(self.buffer_file, "w"), ensure_ascii=False)
        except: pass
    def load_memory(self):
        try: return open(self.memory_file, "r", encoding="utf-8").read()
        except: return ""
    def read_file(self, n): return open(os.path.join(self.base_dir, n), "r", encoding="utf-8").read()
    def check_auth(self, r): return r.query.get("token") == self.local_config.get("web_token")

    async def h_del(self,r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        for f in (await r.json()).get("filenames",[]):
            try: os.remove(os.path.join(self.img_dir,f)); del self.data[f]
            except: pass
        self.save_data(); return web.Response(text="ok")
    async def h_tag(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        d=await r.json(); self.data[d['filename']]['tags']=d['tags']; self.save_data(); return web.Response(text="ok")
    async def h_ucf(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        self.local_config.update(await r.json()); self.save_config(); return web.Response(text="ok")
    async def h_backup(self,r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            for root,_,files in os.walk(self.img_dir): 
                for f in files: z.write(os.path.join(root,f),f"images/{f}")
            if os.path.exists(self.data_file): z.write(self.data_file,"memes.json")
            if os.path.exists(self.memory_file): z.write(self.memory_file,"memory.txt") 
            if os.path.exists(self.config_file): z.write(self.config_file,"config.json") 
        b.seek(0); return web.Response(body=b, headers={'Content-Disposition':'attachment; filename="bk.zip"'})
    async def h_slim(self,r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        loop = asyncio.get_running_loop(); count=0
        for f in os.listdir(self.img_dir):
            try:
                p=os.path.join(self.img_dir,f)
                with open(p,'rb') as fl: raw=fl.read()
                nd, _ = await loop.run_in_executor(self.executor, self.compress_image_sync, raw)
                if len(nd)<len(raw): 
                    with open(p,'wb') as fl: fl.write(nd)
                    count+=1
            except: pass
        return web.Response(text=f"优化了 {count} 张")
    
    # ==========================
    # Web 服务代码 (V10 保持精简)
    # ==========================
    async def start_web_server(self):
        try:
            app = web.Application()
            app._client_max_size = 1024 * 1024 * 1024 
            
            app.router.add_get("/", self.h_idx)
            app.router.add_post("/upload", self.h_up)
            app.router.add_post("/batch_delete", self.h_del)
            app.router.add_post("/update_tag", self.h_tag)
            app.router.add_get("/get_config", self.h_gcf)
            app.router.add_post("/update_config", self.h_ucf)
            app.router.add_get("/backup", self.h_backup)
            app.router.add_post("/restore", self.h_restore)
            app.router.add_post("/slim_images", self.h_slim)
            app.router.add_static("/images/", path=self.img_dir)
            
            runner = web.AppRunner(app)
            await runner.setup()
            port = self.local_config.get("web_port", 5000)
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            print(f"DEBUG: [Meme] WebUI 已启动在端口 {port}")
        except Exception as e:
            print(f"ERROR: [Meme] WebUI 启动失败: {e}")

    async def h_idx(self, r): 
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")
        try:
            current_token = self.local_config.get("web_token", "admin123")
            html = self.read_file("index.html") \
                .replace("{{MEME_DATA}}", json.dumps(self.data)) \
                .replace("admin123", current_token) 
        except:
            html = "Error: index.html not found"
        return web.Response(
            text=html, 
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    async def h_gcf(self, r): return web.json_response(self.local_config)
    async def h_ucf(self, r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            self.local_config.update(await r.json())
            self.save_config()
            return web.Response(text="ok")
        except: return web.Response(status=500)
    async def h_restore(self, r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            field = await reader.next()
            if not field or field.name != 'file': return web.Response(status=400, text="No file")
            file_data = await field.read()
            def unzip_action():
                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as z: z.extractall(self.base_dir)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self.executor, unzip_action)
            self.data = self.load_data()
            self.local_config = self.load_config()
            asyncio.create_task(self._init_image_hashes())
            return web.Response(text="ok")
        except Exception as e: return web.Response(status=500, text=str(e))
    async def h_up(self, r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            default_tag = "未分类"
            while True:
                part = await reader.next()
                if part is None: break
                if part.name == "tags": default_tag = await part.text()
                elif part.name == "file":
                    file_data = await part.read()
                    loop = asyncio.get_running_loop()
                    h = await loop.run_in_executor(self.executor, self.calc_dhash, file_data)
                    comp_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, file_data)
                    fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp_data)
                    self.data[fn] = {"tags": default_tag, "source": "manual"}
                    if h: self.img_hashes[fn] = h
            self.save_data()
            return web.Response(text="ok")
        except Exception as e: return web.Response(status=500, text=str(e))
