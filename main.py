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

# 调试打印：阴历库检测
HAS_LUNAR = False
try:
    from lunar_python import Lunar, Solar
    HAS_LUNAR = True
    print("DEBUG: [Meme] 阴历库 (lunar_python) 加载成功")
except ImportError:
    HAS_LUNAR = False
    print("DEBUG: [Meme] 未检测到 lunar_python，将只显示阳历 (这不影响核心功能)")
except Exception as e:
    HAS_LUNAR = False
    print(f"DEBUG: [Meme] 阴历库加载出错: {e}")

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print("DEBUG: MemeMaster Pro (v3.0 Final Stable) 正在启动...")

@register("vv_meme_master", "MemeMaster", "最终稳定版", "3.0.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.memory_file = os.path.join(self.base_dir, "memory.txt") 
        self.buffer_file = os.path.join(self.base_dir, "buffer.json") 
        
        # 1. 内存保护：单线程
        self.executor = ThreadPoolExecutor(max_workers=1)
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123" 
            self.save_config()

        self.data = self.load_data()
        self.img_hashes = {}
        self.debounce_tasks = {}
        self.msg_buffers = {}
        
        # 2. 恢复缓存
        self.chat_history_buffer = self.load_buffer_from_disk()
        self.last_active_time = time.time()
        self.current_summary = self.load_memory()
        
        self.left_pairs = {'“': '”', '《': '》', '（': '）', '(': ')', '[': ']', '{': '}'}
        self.right_pairs = {v: k for k, v in self.left_pairs.items()}

        # 3. 启动任务
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            loop.create_task(self._init_image_hashes())
            loop.create_task(self._lonely_watcher())
            print("DEBUG: [Meme] 所有后台任务已提交")
        except Exception as e:
            print(f"ERROR: [Meme] 任务启动失败: {e}")

    # ==========================
    # 核心：最稳的上传逻辑
    # ==========================
    async def h_up(self, r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            default_tag = "未分类"
            count = 0
            
            while True:
                part = await reader.next()
                if part is None: break
                
                if part.name == "tags":
                    default_tag = await part.text()
                    continue

                if part.name == "file":
                    # 读取图片数据
                    file_data = await part.read()
                    if not file_data: continue

                    # 放入线程池处理，防止阻塞
                    loop = asyncio.get_running_loop()
                    
                    # 计算哈希
                    h = await loop.run_in_executor(self.executor, self.calc_dhash, file_data)
                    # 压缩
                    compressed_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, file_data)
                    
                    fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(compressed_data)
                    
                    self.data[fn] = {"tags": default_tag, "source": "manual"}
                    if h: self.img_hashes[fn] = h
                    count += 1
                    
                    # 释放内存
                    del file_data, compressed_data
                    gc.collect()

            self.save_data()
            print(f"DEBUG: [Meme] 成功上传 {count} 张图片")
            return web.Response(text="ok")
        except Exception as e:
            print(f"ERROR: [Meme] 上传失败: {e}")
            return web.Response(status=500, text=f"Upload Error: {str(e)}")

    # ==========================
    # Web 服务
    # ==========================
    async def start_web_server(self):
        try:
            app = web.Application()
            app._client_max_size = 50 * 1024 * 1024 # 50MB 限制
            
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

    # ==========================
    # 其他功能 (保持不变)
    # ==========================
    def load_buffer_from_disk(self):
        if os.path.exists(self.buffer_file):
            try:
                with open(self.buffer_file, "r", encoding="utf-8") as f: return json.load(f)
            except: return []
        return []

    def save_buffer_to_disk(self):
        try:
            with open(self.buffer_file, "w", encoding="utf-8") as f:
                json.dump(self.chat_history_buffer, f, ensure_ascii=False, indent=2)
        except: pass

    def get_time_str(self):
        now = datetime.datetime.now()
        week_days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        solar_str = f"{now.strftime('%Y年%m月%d日 %H:%M')} {week_days[now.weekday()]}"
        lunar_str = ""
        if HAS_LUNAR:
            try:
                solar = Solar.fromYmdHms(now.year, now.month, now.day, now.hour, now.minute, now.second)
                lunar = solar.getLunar()
                jieqi = lunar.getJieQi()
                lunar_str = f" 农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}"
                if jieqi: lunar_str += f" ({jieqi})"
            except: pass
        return f"[当前时间: {solar_str}{lunar_str}]"

    async def _lonely_watcher(self):
        # ... (保持之前的逻辑不变)
        while True:
            await asyncio.sleep(60) 
            interval = self.local_config.get("proactive_interval", 0)
            if interval <= 0: continue
            
            q_start = self.local_config.get("quiet_start", -1)
            q_end = self.local_config.get("quiet_end", -1)
            if q_start != -1 and q_end != -1:
                h = datetime.datetime.now().hour
                is_quiet = False
                if q_start > q_end: 
                    if h >= q_start or h < q_end: is_quiet = True
                else:
                    if q_start <= h < q_end: is_quiet = True
                if is_quiet: continue

            if time.time() - self.last_active_time > (interval * 60):
                self.last_active_time = time.time() 
                provider = self.context.get_using_provider()
                uid = getattr(self, "last_uid", None)
                if provider and uid:
                    full_memory = self.load_memory()
                    ctx = f"{self.get_time_str()}\n你已经很久({interval}分钟)没有和用户说话了。\n[你的长期记忆]: {full_memory}\n请根据记忆和时间，主动发起一个不生硬的话题。"
                    try:
                        sid = getattr(self, "last_session_id", None)
                        resp = await provider.text_chat(ctx, session_id=sid)
                        text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                        if text:
                            self.chat_history_buffer.append(f"AI: {text}")
                            self.save_buffer_to_disk()
                            await self.process_and_send(None, text, target_uid=uid)
                    except: pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=50)
    async def handle_private_msg(self, event: AstrMessageEvent):
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
            if uid in self.debounce_tasks: self.debounce_tasks[uid].cancel(); await self._execute_buffer(uid, event)
            return

        debounce_time = self.local_config.get("debounce_time", 5.0)
        if debounce_time <= 0: return

        event.stop_event()

        if uid not in self.msg_buffers: self.msg_buffers[uid] = {'text': [], 'imgs': [], 'event': event}
        self.msg_buffers[uid]['event'] = event 
        if msg_str: self.msg_buffers[uid]['text'].append(msg_str)
        if img_url: self.msg_buffers[uid]['imgs'].append(img_url)

        if uid in self.debounce_tasks and not self.debounce_tasks[uid].done():
            self.debounce_tasks[uid].cancel()

        self.debounce_tasks[uid] = asyncio.create_task(self._debounce_waiter(uid, debounce_time))

    async def _debounce_waiter(self, uid, duration):
        try:
            await asyncio.sleep(duration)
            await self._execute_buffer(uid)
        except asyncio.CancelledError: pass

    async def _execute_buffer(self, uid, force_event=None):
        if uid not in self.msg_buffers: return
        data = self.msg_buffers.pop(uid)
        event = force_event or data['event']
        texts = data['text']
        imgs = data['imgs']
        if not texts and not imgs: return
        
        image_urls = [url for url in imgs]
        user_input = "\n".join(texts)
        
        asyncio.create_task(self.check_and_summarize())

        time_info = self.get_time_str()
        memory_info = f"\n[前情提要: {self.current_summary}]" if self.current_summary else ""
        
        hint_msg = ""
        if random.randint(1, 100) <= self.local_config.get("reply_prob", 50):
            all_tags = [v.get("tags", "").split(":")[0].strip() for v in self.data.values()]
            if all_tags:
                hints = random.sample(all_tags, min(15, len(all_tags)))
                hint_str = " ".join([f"<MEME:{h}>" for h in hints])
                hint_msg = f"\n[可用表情包: {hint_str}]\n回复格式: <MEME:名称>"

        full_prompt = f"{time_info}{memory_info}\n{user_input}{hint_msg}"
        event.message_str = full_prompt
        
        print(f"[Meme] 请求LLM...")
        provider = self.context.get_using_provider()
        if provider:
            try:
                resp = await provider.text_chat(text=full_prompt, session_id=event.session_id, image_urls=image_urls)
                reply = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                if reply: 
                    self.chat_history_buffer.append(f"AI: {reply}")
                    self.save_buffer_to_disk()
                    await self.process_and_send(event, reply)
            except Exception as e: print(f"LLM请求失败: {e}")

    def load_memory(self):
        if os.path.exists(self.memory_file):
            try: return open(self.memory_file, "r", encoding="utf-8").read()
            except: return ""
        return ""

    async def check_and_summarize(self):
        threshold = self.local_config.get("summary_threshold", 50) 
        if len(self.chat_history_buffer) < threshold: return

        current_batch = list(self.chat_history_buffer)
        history_text = "\n".join(current_batch)
        
        print(f"[Meme] 触发记忆总结...")
        provider = self.context.get_using_provider()
        if not provider: return

        now_str = self.get_time_str()
        prompt = f"""当前时间：{now_str}
这是最近的{len(current_batch)}句对话。请总结成一段“日记”，追加到长期记忆中。
要求：包含准确时间信息，记录关键事件、用户偏好、重要事。忽略无意义寒暄。200字以内。
对话内容：
{history_text}"""

        try:
            resp = await provider.text_chat(prompt, session_id=None)
            summary = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if summary:
                def write_task():
                    with open(self.memory_file, "a", encoding="utf-8") as f: 
                        f.write(f"\n\n--- {now_str} ---\n{summary}")
                await asyncio.get_running_loop().run_in_executor(self.executor, write_task)
                
                self.current_summary = self.load_memory()
                self.chat_history_buffer = self.chat_history_buffer[len(current_batch):]
                self.save_buffer_to_disk()
                print(f"[Meme] 记忆追加成功")
                gc.collect()
        except Exception as e:
            print(f"总结失败 ({e})")
            if len(self.chat_history_buffer) > threshold * 2:
                self.chat_history_buffer = self.chat_history_buffer[threshold:]
                self.save_buffer_to_disk()

    async def process_and_send(self, event, text, target_uid=None):
        text = text.replace("**", "").replace("### ", "")
        print(f"[Meme] AI回复: {text[:30]}...")
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
                    if part.strip().startswith(":") and len(part) < 30: continue
                    mixed_chain.append(Plain(part))
            
            segments = self.smart_split(mixed_chain)
            uid = target_uid or event.unified_msg_origin
            
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_c = "".join([c.text for c in seg if isinstance(c, Plain)])
                mc = MessageChain(); mc.chain = seg
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
                if w > h: new_w = max_size; new_h = int(h * (max_size / w))
                else: new_h = max_size; new_w = int(w * (max_size / h))
                img = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
            
            buffer = io.BytesIO()
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                if img.mode != "RGBA": img = img.convert("RGBA")
                img.save(buffer, format="PNG", optimize=True); return buffer.getvalue(), ".png"
            else:
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buffer, format="JPEG", quality=70, optimize=True); return buffer.getvalue(), ".jpg"
        except: return image_data, ".jpg"

    async def ai_evaluate_image(self, img_url, context_text=""):
        try:
            self.last_auto_save_time = time.time()
            img_data = await self.download_image(img_url)
            if not img_data: return
            if len(img_data) > 4 * 1024 * 1024: return

            loop = asyncio.get_running_loop()
            current_hash = await loop.run_in_executor(self.executor, self.calc_dhash, img_data)
            if current_hash and self.is_duplicate(current_hash): return
            provider = self.context.get_using_provider()
            if not provider: return
            default_prompt = """你正在整理表情包。用户配文：“{context_text}”。
规则：
1. 二次元/Meme环境，严禁幻觉。黑名单：米哈游/原神/孙笑川/辱女。
2. 若保存，格式：YES\n<MEME:名称>: 简短说明"""
            prompt_template = self.local_config.get("ai_prompt", default_prompt)
            prompt = prompt_template.replace("{context_text}", context_text)
            resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
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
            gc.collect() 
        except: pass

    async def _init_image_hashes(self):
        loop = asyncio.get_running_loop()
        count = 0
        print("DEBUG: [Meme] 开始扫描图片哈希...")
        
        # 获取所有文件列表
        if not os.path.exists(self.img_dir): return
        files = os.listdir(self.img_dir)
        
        for f in files:
            # 只看图片
            if not f.lower().endswith(('.jpg', '.png', '.jpeg', '.gif', '.webp')): continue
            
            path = os.path.join(self.img_dir, f)
            try:
                # 读文件
                with open(path, "rb") as fl: 
                    content = fl.read()
                    # 扔给后台计算，不卡顿
                    h = await loop.run_in_executor(self.executor, self.calc_dhash, content)
                    if h: 
                        self.img_hashes[f] = h
                        count += 1
            except PermissionError:
                print(f"ERROR: 权限不足，无法读取文件: {f} (请检查 Docker 权限)")
            except Exception:
                pass # 其他小错误跳过
        
        print(f"DEBUG: [Meme] 哈希扫描完成，共索引 {count} 张图片")

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
    
    def is_duplicate(self, h, t=5):
        if not h: return False
        for _, eh in self.img_hashes.items():
            try:
                if bin(int(h, 16) ^ int(eh, 16)).count('1') <= t: return True
            except: continue
        return False

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

    def _get_img_url(self, e):
        for c in e.message_obj.message:
            if isinstance(c, Image): return c.url
        return None
    def load_config(self): return {**{"web_port":5000,"debounce_time":5.0,"reply_prob":50,"proactive_interval":0,"summary_threshold":50}, **(json.load(open(self.config_file)) if os.path.exists(self.config_file) else {})}
    def save_config(self): json.dump(self.local_config, open(self.config_file,"w"), indent=2)
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)

    def check_auth(self, r):
        token = r.query.get("token")
        if token == self.local_config.get("web_token"): return True
        return False

    async def h_idx(self, r): 
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")
        
        # 这一步是把数据填进网页
        html = self.read_file("index.html").replace("{{MEME_DATA}}", json.dumps(self.data))
        
        # 这一步是关键：告诉浏览器“千万别缓存，每次都找服务器要最新的”
        return web.Response(
            text=html, 
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    
    async def h_del(self,r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        for f in (await r.json()).get("filenames",[]):
            try: os.remove(os.path.join(self.img_dir,f)); del self.data[f]
            except: pass
        self.save_data(); return web.Response(text="ok")
    async def h_tag(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        d=await r.json(); self.data[d['filename']]['tags']=d['tags']; self.save_data(); return web.Response(text="ok")
    async def h_gcf(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        return web.json_response(self.local_config)
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
    
    async def h_restore(self, r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            field = await reader.next()
            if not field or field.name != 'file': 
                return web.Response(status=400, text="No file")
            
            # 1. 像旧版一样，直接读取所有数据到内存（你只有5M，这完全没问题）
            file_data = await field.read()
            
            # 2. 定义解压动作（为了不卡死机器人，我们把它包一下）
            def unzip_action():
                # 这里的逻辑和你的旧版一模一样
                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as z:
                    z.extractall(self.base_dir)
            
            # 3. 扔到后台线程去跑
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self.executor, unzip_action)

            # 4. 刷新数据
            self.data = self.load_data()
            self.local_config = self.load_config()
            
            # 5. 顺手重新计算一下哈希
            asyncio.create_task(self._init_image_hashes())
            
            return web.Response(text="ok")
        except Exception as e:
            # 打印错误到控制台，方便你看 docker logs
            print(f"ERROR: 恢复备份失败: {e}")
            return web.Response(status=500, text=f"Restore Failed: {str(e)}")
    async def h_slim(self,r):
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        count = 0; loop = asyncio.get_running_loop()
        self.img_hashes = {}
        for f in os.listdir(self.img_dir):
            path = os.path.join(self.img_dir, f)
            try:
                with open(path, 'rb') as file: raw = file.read()
                old_size = len(raw)
                h = await loop.run_in_executor(self.executor, self.calc_dhash, raw)
                if h: self.img_hashes[f] = h
                new_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, raw)
                if len(new_data) < old_size:
                    with open(path, 'wb') as file: file.write(new_data)
                    count += 1
            except: pass
        return web.Response(text=f"优化 {count} 张")
    
    def read_file(self, n): return open(os.path.join(self.base_dir, n), "r", encoding="utf-8").read()
