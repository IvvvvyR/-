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
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from PIL import Image as PILImage

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print("DEBUG: MemeMaster Pro (Final V14) - Integrity Checked")

@register("vv_meme_master", "MemeMaster", "最终完全体V14", "4.2.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.memory_file = os.path.join(self.base_dir, "memory.txt") 
        self.buffer_file = os.path.join(self.base_dir, "buffer.json") 
        
        self.executor = ThreadPoolExecutor(max_workers=2)
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123"
            self.save_config()

        self.data = self.load_data()
        
        # 运行时状态
        self.chat_history_buffer = self.load_buffer_from_disk()
        self.current_summary = self.load_memory()
        self.msg_count = 0
        self.img_hashes = {} # 内存指纹索引
        self.sessions = {} 
        self.is_summarizing = False # [补漏] 总结锁，防止并发冲突
        
        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '[': ']', '{': '}'}
        self.right_pairs = {v: k for k, v in self.pair_map.items()}

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            loop.create_task(self._init_image_hashes()) # [补漏] 优化后的指纹加载
        except Exception as e:
            print(f"ERROR: 任务启动失败: {e}")

    # ===============================================================
    # 核心逻辑 1：输入端 (多图防抖 + 自动进货 + 注入)
    # ===============================================================
    async def _debounce_timer(self, uid: str, duration: float):
        try:
            await asyncio.sleep(duration)
            if uid in self.sessions: 
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError: pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=50)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=50)
    async def handle_input(self, event: AstrMessageEvent):
        try:
            if str(event.message_obj.sender.user_id) == str(self.context.get_current_provider_bot().self_id): return
        except: pass

        msg_str = (event.message_str or "").strip()
        img_urls = self._get_all_img_urls(event)
        uid = event.unified_msg_origin

        # 1. 暗线：自动进货 (批量处理 + 冷却控制)
        if img_urls and not msg_str.startswith("/"):
            cooldown = self.local_config.get("auto_save_cooldown", 60)
            if time.time() - getattr(self, "last_auto_save_time", 0) > cooldown:
                # 触发冷却，处理该批次所有图片
                self.last_auto_save_time = time.time()
                print(f"🕵️ [Meme] 自动进货冷却完毕，后台鉴定 {len(img_urls)} 张图...")
                for url in img_urls:
                    asyncio.create_task(self.ai_evaluate_image(url))

        # 2. 指令穿透
        if msg_str.startswith(("/", "！", "!")):
            if uid in self.sessions:
                if self.sessions[uid].get('timer_task'): self.sessions[uid]['timer_task'].cancel()
                self.sessions[uid]['flush_event'].set()
            return

        # 3. 防抖逻辑
        debounce_time = self.local_config.get("debounce_time", 3.0)
        if debounce_time <= 0: return 

        if uid in self.sessions:
            s = self.sessions[uid]
            if msg_str: s['buffer'].append(msg_str)
            if img_urls: s['images'].extend(img_urls)
            if s.get('timer_task'): s['timer_task'].cancel()
            s['timer_task'] = asyncio.create_task(self._debounce_timer(uid, debounce_time))
            event.stop_event()
            return

        flush_event = asyncio.Event()
        timer_task = asyncio.create_task(self._debounce_timer(uid, debounce_time))
        self.sessions[uid] = {
            'buffer': [msg_str] if msg_str else [],
            'images': img_urls if img_urls else [],
            'flush_event': flush_event,
            'timer_task': timer_task
        }
        
        print(f"🕒 [Meme] 消息防抖中 ({debounce_time}s)...")
        await flush_event.wait()

        if uid not in self.sessions: return
        s = self.sessions.pop(uid)
        merged_text = " ".join(s['buffer']).strip()
        
        if not merged_text and not s['images']: return

        # 4. 记录 User Buffer
        img_mark = f" [Image*{len(s['images'])}]" if s['images'] else ""
        self.chat_history_buffer.append(f"User: {merged_text}{img_mark}")
        self.save_buffer_to_disk()

        # 5. 上下文注入 (时间 + 表情包提示 + 记忆)
        self.msg_count += 1
        inject_interval = self.local_config.get("memory_interval", 20)
        should_inject_memory = (self.msg_count % inject_interval == 0) or (self.msg_count == 1)
        
        time_info = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        system_note_parts = [f"Time: {time_info}"]
        
        if should_inject_memory and self.current_summary:
            print(f"🧠 [Meme] 注入长期记忆 (第{self.msg_count}轮)...")
            system_note_parts.append(f"Long-term Memory: {self.current_summary}")
        
        if random.randint(1, 100) <= self.local_config.get("reply_prob", 50):
            all_tags = [v.get("tags", "").split(":")[0].strip() for v in self.data.values()]
            if all_tags:
                hints = random.sample(all_tags, min(15, len(all_tags)))
                hint_str = " ".join([f"<MEME:{h}>" for h in hints])
                system_note_parts.append(f"Meme Hints: {hint_str}")
        
        system_note_str = " | ".join(system_note_parts)
        final_text = f"{merged_text}\n\n(System Context: {system_note_str})"
        
        # 6. 放行给 AstrBot
        chain = [Plain(final_text)]
        for url in s['images']:
            chain.append(Image.fromURL(url))
            
        event.message_str = final_text
        event.message_obj.message = chain
        
        print(f"🚀 [Meme] 放行给 AstrBot")

    # ===============================================================
    # 核心逻辑 2：输出端 (拦截 -> 触发总结 -> 分段发送)
    # ===============================================================
    @filter.on_decorating_result(priority=0)
    async def on_output(self, event: AstrMessageEvent):
        if getattr(event, "__meme_processed", False): return
        
        result = event.get_result()
        if not result: return
        
        text = ""
        if isinstance(result, list):
            for c in result:
                if isinstance(c, Plain): text += c.text
        elif hasattr(result, "chain"):
            for c in result.chain:
                if isinstance(c, Plain): text += c.text
        else: text = str(result)
            
        if not text: return
        setattr(event, "__meme_processed", True)
        
        print(f"📤 [Meme] 捕获回复: {text[:30]}...")

        # 1. 记录 AI Buffer
        clean_text = re.sub(r"\(System Context:.*?\)", "", text).strip()
        self.chat_history_buffer.append(f"AI: {clean_text}")
        self.save_buffer_to_disk()
        
        # 2. 触发总结 (带锁检测)
        if not self.is_summarizing:
            asyncio.create_task(self.check_and_summarize())

        try:
            # 3. 解析与分段
            pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
            parts = re.split(pattern, text)
            mixed_chain = []
            has_meme = False
            
            for part in parts:
                tag = None
                if part.startswith("<MEME:"): tag = part[6:-1].strip()
                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                
                if tag:
                    path = self.find_best_match(tag)
                    if path: 
                        mixed_chain.append(Image.fromFileSystem(path))
                        has_meme = True
                elif part:
                    clean_part = part.replace("(System Context:", "").replace(")", "").strip()
                    if clean_part: mixed_chain.append(Plain(clean_part))
            
            if not has_meme and len(text) < 50 and "\n" not in text: return

            segments = self.smart_split(mixed_chain)
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                wait = delay_base + (txt_len * delay_factor)
                
                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                if i < len(segments) - 1: await asyncio.sleep(wait)
            
            event.set_result(None)

        except Exception as e:
            print(f"❌ [Meme] 输出处理出错: {e}")

    # ===============================================================
    # 功能逻辑：自动鉴图 (指纹检测 + 鉴权)
    # ===============================================================
    async def ai_evaluate_image(self, img_url):
        try:
            # 1. 下载
            img_data = None
            async with aiohttp.ClientSession() as s:
                async with s.get(img_url) as r:
                    if r.status == 200: img_data = await r.read()
            if not img_data: return

            # 2. 计算指纹
            current_hash = await self._calc_hash_async(img_data)

            # 3. 指纹查重 (省 Token)
            if current_hash:
                for _, exist_hash in self.img_hashes.items():
                    # 汉明距离 <= 5 视为相同
                    if bin(int(current_hash, 16) ^ int(exist_hash, 16)).count('1') <= 5:
                        print(f"♻️ [自动进货] 图片已存在，跳过。")
                        return

            # 4. LLM 鉴定
            provider = self.context.get_using_provider()
            if not provider: return
            
            # 使用配置的Prompt
            default_prompt = "判断这张图是否适合做表情包(二次元/Meme)。适合回YES并给出<名称>:说明，不适合回NO。"
            prompt = self.local_config.get("ai_prompt", default_prompt)
            
            resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
            content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if "YES" in content:
                match = re.search(r"<(?P<tag>.*?)>[:：]?(?P<desc>.*)", content)
                if match:
                    full_tag = f"{match.group('tag').strip()}: {match.group('desc').strip()}"
                    print(f"🖤 [自动进货] {full_tag}")
                    
                    comp, ext = await self._compress_image(img_data)
                    fn = f"{int(time.time())}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                    
                    self.data[fn] = {"tags": full_tag, "source": "auto", "hash": current_hash}
                    if current_hash: self.img_hashes[fn] = current_hash
                    self.save_data()
        except Exception as e:
            print(f"鉴图出错: {e}")

    # ===============================================================
    # 功能逻辑：记忆总结 (带锁)
    # ===============================================================
    async def check_and_summarize(self):
        threshold = self.local_config.get("summary_threshold", 40)
        if len(self.chat_history_buffer) < threshold: return
        
        self.is_summarizing = True # 上锁
        
        try:
            batch_to_summarize = list(self.chat_history_buffer)
            history_text = "\n".join(batch_to_summarize)
            
            print(f"📖 [Meme] 开始后台总结 (条数:{len(batch_to_summarize)})...")
            provider = self.context.get_using_provider()
            if not provider: return

            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            prompt = f"""当前时间：{now_str}
                这是一段过去的对话记录。请将其总结为一段简练的“长期记忆”或“日记”。
                重点记录：用户的喜好、发生的重要事件、双方约定的事情。
                忽略：无意义的寒暄、重复的表情包指令。
                字数限制：200字以内。
                对话内容：
                {history_text}"""

            resp = await provider.text_chat(prompt, session_id=None)
            summary = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if summary:
                def write_task():
                    with open(self.memory_file, "a", encoding="utf-8") as f: 
                        f.write(f"\n\n--- {now_str} ---\n{summary}")
                await asyncio.get_running_loop().run_in_executor(self.executor, write_task)
                
                self.current_summary = self.load_memory()
                self.chat_history_buffer = self.chat_history_buffer[len(batch_to_summarize):]
                self.save_buffer_to_disk()
                print(f"✅ [Meme] 总结完成，Buffer已清理")
        except Exception as e:
            print(f"❌ [Meme] 总结失败: {e}")
            if len(self.chat_history_buffer) > 100: # 兜底清理
                self.chat_history_buffer = self.chat_history_buffer[-50:]
                self.save_buffer_to_disk()
        finally:
            self.is_summarizing = False # 解锁

    # ==========================
    # 工具函数 (指纹缓存优化)
    # ==========================
    async def _init_image_hashes(self):
        print("DEBUG: [Meme] 正在构建指纹索引 (带缓存)...")
        loop = asyncio.get_running_loop()
        count = 0
        recalc = 0
        
        if not os.path.exists(self.img_dir): return
        files = os.listdir(self.img_dir)
        
        for f in files:
            if not f.lower().endswith(('.jpg', '.png', '.jpeg', '.gif', '.webp')): continue
            
            # [补漏] 优先从 self.data 读取缓存的 hash，避免 IO 爆炸
            if f in self.data and 'hash' in self.data[f] and self.data[f]['hash']:
                self.img_hashes[f] = self.data[f]['hash']
                count += 1
                continue
            
            # 没缓存才计算
            try:
                path = os.path.join(self.img_dir, f)
                with open(path, "rb") as fl: content = fl.read()
                h = await self._calc_hash_async(content)
                if h: 
                    self.img_hashes[f] = h
                    # 回写到 data
                    if f not in self.data: self.data[f] = {"tags": "未分类", "source": "unknown"}
                    self.data[f]['hash'] = h
                    count += 1
                    recalc += 1
            except: pass
            
        if recalc > 0: self.save_data()
        print(f"DEBUG: [Meme] 指纹构建完成 (总数:{count}, 新算:{recalc})")

    async def _calc_hash_async(self, image_data):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): img.seek(0)
                img = img.resize((9, 8), PILImage.Resampling.LANCZOS).convert('L')
                pixels = list(img.getdata())
                val = sum(2**i for i, v in enumerate([pixels[row*9+col] > pixels[row*9+col+1] for row in range(8) for col in range(8)]) if v)
                return hex(val)[2:]
            except: return None
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    async def _compress_image(self, image_data: bytes):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): return image_data, ".gif"
                max_w = 400
                if img.width > max_w:
                    ratio = max_w / img.width
                    img = img.resize((max_w, int(img.height * ratio)), PILImage.Resampling.LANCZOS)
                buf = io.BytesIO()
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue(), ".jpg"
            except: return image_data, ".jpg"
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    def _get_all_img_urls(self, e):
        urls = []
        for c in e.message_obj.message:
            if isinstance(c, Image): urls.append(c.url)
        return urls
    
    def _get_img_url(self, e): # 兼容旧接口
        urls = self._get_all_img_urls(e)
        return urls[0] if urls else None

    def smart_split(self, chain):
        segs = []; buf = []
        def flush(): 
            if buf: segs.append(buf[:]); buf.clear()
        for c in chain:
            if isinstance(c, Image): flush(); segs.append([c]); continue
            if isinstance(c, Plain):
                txt = c.text; idx = 0; chunk = ""; stack = []
                while idx < len(txt):
                    char = txt[idx]
                    if char in self.pair_map: stack.append(char)
                    elif stack and char == self.pair_map[stack[-1]]: stack.pop()
                    if not stack and char in "\n。？！?!":
                        chunk += char
                        while idx + 1 < len(txt) and txt[idx+1] in "\n。？！?!": idx += 1; chunk += txt[idx]
                        if chunk.strip(): buf.append(Plain(chunk))
                        flush(); chunk = ""
                    else: chunk += char
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        flush(); return segs

    def find_best_match(self, query):
        best, score = None, 0
        for f, i in self.data.items():
            t = i.get("tags", "")
            if query in t: return os.path.join(self.img_dir, f)
            s = difflib.SequenceMatcher(None, query, t.split(":")[0]).ratio()
            if s > score: score = s; best = f
        if score > 0.4: return os.path.join(self.img_dir, best)
        return None

    def load_config(self): 
        default = {"web_port":5000, "debounce_time":3.0, "reply_prob":50, "auto_save_cooldown":60, "memory_interval": 20, "summary_threshold": 40}
        if os.path.exists(self.config_file):
            try: default.update(json.load(open(self.config_file)))
            except: pass
        return default
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

    # ==========================
    # Web Server
    # ==========================
    async def start_web_server(self):
        app = web.Application(); app._client_max_size = 100*1024*1024 
        app.router.add_get("/", self.h_idx); app.router.add_post("/upload", self.h_up)
        app.router.add_post("/batch_delete", self.h_del); app.router.add_post("/update_tag", self.h_tag)
        app.router.add_get("/get_config", self.h_gcf); app.router.add_post("/update_config", self.h_ucf)
        app.router.add_get("/backup", self.h_backup); app.router.add_post("/restore", self.h_restore)
        app.router.add_static("/images/", path=self.img_dir)
        runner = web.AppRunner(app); await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"DEBUG: [Meme] WebUI started at port {port}")

    async def h_idx(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")
        token = self.local_config["web_token"]
        html = self.read_file("index.html").replace("{{MEME_DATA}}", json.dumps(self.data)).replace("admin123", token)
        return web.Response(text=html, content_type="text/html")
    async def h_up(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        rd = await r.multipart(); tag="未分类"
        while True:
            p = await rd.next()
            if not p: break
            if p.name == "tags": tag = await p.text()
            elif p.name == "file":
                raw = await p.read()
                comp, ext = await self._compress_image(raw)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                h = await self._calc_hash_async(comp) # 计算并保存hash
                self.data[fn] = {"tags": tag, "source": "manual", "hash": h}
                if h: self.img_hashes[fn] = h
        self.save_data(); return web.Response(text="ok")
    async def h_del(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        for f in (await r.json()).get("filenames",[]):
            try: os.remove(os.path.join(self.img_dir,f)); del self.data[f]; self.img_hashes.pop(f, None)
            except: pass
        self.save_data(); return web.Response(text="ok")
    async def h_tag(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        d=await r.json(); self.data[d['filename']]['tags']=d['tags']; self.save_data(); return web.Response(text="ok")
    async def h_gcf(self,r): return web.json_response(self.local_config)
    async def h_ucf(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        self.local_config.update(await r.json()); self.save_config(); return web.Response(text="ok")
    async def h_backup(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            for root,_,files in os.walk(self.img_dir): 
                for f in files: z.write(os.path.join(root,f),f"images/{f}")
            z.write(self.data_file,"memes.json"); z.write(self.config_file,"config.json")
        b.seek(0); return web.Response(body=b, headers={'Content-Disposition':'attachment; filename="bk.zip"'})
    async def h_restore(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        rd = await r.multipart(); f = await rd.next()
        if f.name != 'file': return web.Response(status=400)
        dat = await f.read()
        def unzip(): 
            with zipfile.ZipFile(io.BytesIO(dat),'r') as z: z.extractall(self.base_dir)
        await asyncio.get_running_loop().run_in_executor(self.executor, unzip)
        self.data=self.load_data(); self.local_config=self.load_config()
        return web.Response(text="ok")
