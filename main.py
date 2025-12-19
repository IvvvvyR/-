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
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web

from PIL import Image as PILImage

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print("DEBUG: MemeMaster Pro (v1.4.0 Fingerprint) 已加载")

@register("vv_meme_master", "MemeMaster", "防抖+表情包+去重指纹", "1.4.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        
        self.executor = ThreadPoolExecutor(max_workers=3)
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        self.data = self.load_data()
        
        # 指纹缓存库 {filename: dhash_string}
        self.img_hashes = {} 
        
        self.sessions = {}
        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '[': ']', '{': '}'}

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            # 启动时后台建立指纹库
            loop.create_task(self._init_image_hashes())
        except Exception as e:
            print(f"ERROR: 启动任务失败: {e}")

    # ==========================
    # 核心功能：视觉指纹 (dHash)
    # ==========================
    async def _init_image_hashes(self):
        """后台任务：计算现有图片的指纹"""
        print("[Meme] 正在建立图片指纹库 (去重用)...")
        count = 0
        loop = asyncio.get_running_loop()
        files = os.listdir(self.img_dir)
        
        for f in files:
            # 简单过滤非图片
            if not f.lower().endswith(('.jpg', '.png', '.jpeg', '.gif', '.webp')): continue
            
            path = os.path.join(self.img_dir, f)
            try:
                with open(path, "rb") as file:
                    data = file.read()
                # 扔到线程池计算，防止卡顿
                h = await loop.run_in_executor(self.executor, self.calc_dhash, data)
                if h: self.img_hashes[f] = h
                count += 1
            except: pass
            
        print(f"[Meme] 指纹库建立完成，共索引 {count} 张图片")

    def calc_dhash(self, image_data: bytes) -> str:
        """计算图片的差异哈希 (dHash)"""
        try:
            # 1. 打开图片
            if isinstance(image_data, bytes):
                img = PILImage.open(io.BytesIO(image_data))
            else:
                return None
                
            # 2. 缩放到 9x8 并转灰度
            img = img.resize((9, 8), PILImage.Resampling.LANCZOS).convert('L')
            
            # 3. 计算差异
            pixels = list(img.getdata())
            diff = []
            for row in range(8):
                for col in range(8):
                    idx = row * 9 + col
                    # 如果左边像素比右边亮，记为1，否则0
                    diff.append(pixels[idx] > pixels[idx + 1])
            
            # 4. 转十六进制字符串
            decimal_value = 0
            for index, value in enumerate(diff):
                if value: decimal_value += 2**index
            return hex(decimal_value)[2:]
            
        except Exception:
            return None

    def is_duplicate(self, new_hash: str, threshold=5) -> bool:
        """检查是否存在相似图片 (汉明距离 < 阈值)"""
        if not new_hash: return False
        
        for filename, existing_hash in self.img_hashes.items():
            # 计算汉明距离
            try:
                dist = bin(int(new_hash, 16) ^ int(existing_hash, 16)).count('1')
                if dist <= threshold:
                    # print(f"[Meme] 发现重复: 与 {filename} 相似度高 (dist={dist})")
                    return True
            except: continue
        return False

    # ==========================
    # 工具：图片下载与压缩
    # ==========================
    def compress_image_sync(self, image_data: bytes) -> tuple[bytes, str]:
        try:
            img = PILImage.open(io.BytesIO(image_data))
            max_size = 350 
            w, h = img.size
            if w > max_size or h > max_size:
                if w > h:
                    new_w = max_size; new_h = int(h * (max_size / w))
                else:
                    new_h = max_size; new_w = int(w * (max_size / h))
                img = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
            
            buffer = io.BytesIO()
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                if img.mode != "RGBA": img = img.convert("RGBA")
                img.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue(), ".png"
            else:
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buffer, format="JPEG", quality=70, optimize=True)
                return buffer.getvalue(), ".jpg"
        except Exception as e:
            return image_data, ".jpg"

    async def download_image(self, url):
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200: return await resp.read()
            return None
        except: return None

    # ==========================
    # 核心 1: 输入端防抖
    # ==========================
    async def _timer_coroutine(self, uid: str, duration: float):
        try:
            await asyncio.sleep(duration)
            if uid in self.sessions: self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError: pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=50)
    async def handle_private_msg(self, event: AstrMessageEvent):
        try:
            sender_id = str(event.message_obj.sender.user_id)
            bot_self_id = str(self.context.get_current_provider_bot().self_id)
            if sender_id == bot_self_id: return
        except: pass

        try:
            msg_str = (event.message_str or "").strip()
            img_url = self._get_img_url(event)
            uid = event.unified_msg_origin

            if not msg_str and not img_url: return

            # 暗线：自动进货 (逻辑优化：先判断指纹，再问AI)
            if img_url and not msg_str and not msg_str.startswith("/"):
                cooldown = self.local_config.get("auto_save_cooldown", 60)
                last_save = getattr(self, "last_auto_save_time", 0)
                if time.time() - last_save > cooldown:
                    print(f"[Meme] 收到图片，准备后台鉴定...")
                    asyncio.create_task(self.ai_evaluate_image(img_url))

            # 指令穿透
            if msg_str.startswith("/") or msg_str.startswith("！") or msg_str.startswith("!"):
                if uid in self.sessions:
                    if self.sessions[uid].get('timer_task'): self.sessions[uid]['timer_task'].cancel()
                    self.sessions[uid]['flush_event'].set()
                return

            debounce_time = self.local_config.get("debounce_time", 2.0)
            if debounce_time <= 0: return

            # A. 续杯
            if uid in self.sessions:
                s = self.sessions[uid]
                if msg_str: s['queue'].append({'type':'text', 'content':msg_str})
                if img_url: s['queue'].append({'type':'image', 'url':img_url})
                if s.get('timer_task'): s['timer_task'].cancel()
                s['timer_task'] = asyncio.create_task(self._timer_coroutine(uid, debounce_time))
                event.stop_event()
                return

            # B. 新杯
            flush_event = asyncio.Event()
            timer_task = asyncio.create_task(self._timer_coroutine(uid, debounce_time))
            initial_queue = []
            if msg_str: initial_queue.append({'type':'text', 'content':msg_str})
            if img_url: initial_queue.append({'type':'image', 'url':img_url})

            self.sessions[uid] = {
                'queue': initial_queue, 'flush_event': flush_event, 'timer_task': timer_task
            }
            
            print(f"[Meme] 开始防抖 ({debounce_time}s)...")
            await flush_event.wait() 

            # C. 结算
            if uid not in self.sessions: return
            s = self.sessions.pop(uid)
            queue = s['queue']
            if not queue: return

            print(f"[Meme] 防抖结束，处理 {len(queue)} 个片段...")

            new_chain = []
            full_text_buffer = []
            loop = asyncio.get_running_loop()

            for item in queue:
                if item['type'] == 'text':
                    new_chain.append(Plain(item['content']))
                    full_text_buffer.append(item['content'])
                elif item['type'] == 'image':
                    img_data = await self.download_image(item['url'])
                    if img_data:
                        comp_data, _ = await loop.run_in_executor(self.executor, self.compress_image_sync, img_data)
                        new_chain.append(Image.fromBytes(comp_data))

            merged_text = "\n".join(full_text_buffer)
            if merged_text and random.randint(1, 100) <= self.local_config.get("reply_prob", 50):
                all_tags = [i.get("tags") for i in self.data.values()]
                if all_tags:
                    hint_tags = "、".join(random.sample(all_tags, min(20, len(all_tags))))
                    merged_text += f"\n\n[System]\nAvailable Memes: {hint_tags}\nTo use, reply: MEME_TAG:tag_name"

            event.message_str = merged_text
            event.message_obj.message = new_chain
            print(f"[Meme] 放行消息给LLM: {merged_text[:30]}...")
            
        except Exception as e:
            print(f"ERROR inside handler: {e}")
            return

    # ==========================
    # 核心 2: 输出端
    # ==========================
    @filter.on_decorating_result(priority=0)
    async def on_decorate(self, event: AstrMessageEvent):
        if getattr(event, "__processed", False): return
        
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
        setattr(event, "__processed", True)
        
        print(f"[Meme] AI回复: {text[:30]}...")

        try:
            parts = re.split(r"(MEME_TAG:\s*[\S]+)", text)
            mixed_chain = []
            
            for part in parts:
                if "MEME_TAG:" in part:
                    tag = part.replace("MEME_TAG:", "").strip()
                    path = self.find_best_match(tag)
                    if path: 
                        print(f"🎯 命中图片: {tag}")
                        mixed_chain.append(Image.fromFileSystem(path))
                    else: pass 
                elif part:
                    mixed_chain.append(Plain(part))
            
            segments = self.smart_split(mixed_chain)
            
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_content = "".join([c.text for c in seg if isinstance(c, Plain)])
                wait = delay_base + (len(txt_content) * delay_factor)
                
                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                
                if i < len(segments) - 1: await asyncio.sleep(wait)
            
            event.set_result(None)

        except Exception as e:
            print(f"分段发送出错: {e}")

    # ==========================
    # 核心 3: 自动进货 (加入去重)
    # ==========================
    async def ai_evaluate_image(self, img_url):
        try:
            self.last_auto_save_time = time.time()
            
            # 1. 先下载原图
            img_data = await self.download_image(img_url)
            if not img_data: return

            # 2. 计算指纹并去重
            loop = asyncio.get_running_loop()
            current_hash = await loop.run_in_executor(self.executor, self.calc_dhash, img_data)
            
            if current_hash and self.is_duplicate(current_hash):
                print(f"💛 [自动进货] 图片已存在 (指纹匹配)，跳过")
                return # 直接结束，不问AI

            # 3. 询问 AI
            provider = self.context.get_using_provider()
            if not provider: return
            
            prompt = """你正在帮我整理一个 QQ 表情包素材库。
请判断这张图片是否“值得被保存”，
作为未来聊天中可能会使用的表情包素材。
判断时请注意：
- 这是一个偏二次元 / meme 使用环境
- 常见来源包括：chiikawa、这狗、线条小狗、多栋、猫meme 等
- 不要过度严肃，也不要把普通照片当成表情包
如果这张图不适合做表情包，请只回复：NO
如果适合，请严格按下面格式回复：
YES
<名称>:<一句自然语言解释这个表情包在什么语境下使用>"""

            # 既然已经下载了，其实可以直接传 URL 比较快（节省上传带宽）
            # 或者如果 Provider 支持 base64 也可以传 img_data
            # 为了兼容性，我们还是传 URL 给 AI
            resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
            content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if "YES" in content:
                lines = content.split('\n')
                tag_line = lines[-1].strip()
                if ":" in tag_line:
                    tag = tag_line.split(":")[0].replace("<", "").replace(">", "").strip()
                    desc = tag_line.split(":")[-1].strip()
                    full_tag = f"{tag}: {desc}"
                    print(f"🖤 [自动进货] {full_tag}")
                    
                    # 4. 保存
                    comp_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, img_data)
                    fn = f"{int(time.time())}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp_data)
                    
                    self.data[fn] = {"tags": full_tag, "source": "auto"}
                    if current_hash: self.img_hashes[fn] = current_hash # 更新内存指纹库
                    self.save_data()
        except Exception as e:
            print(f"鉴图出错: {e}")

    # ==========================
    # Web Server & Helpers
    # ==========================
    async def start_web_server(self):
        app = web.Application()
        app._client_max_size = 50 * 1024 * 1024 
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
        runner = web.AppRunner(app); await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"WebUI: http://localhost:{port}")

    async def h_idx(self,r): return web.Response(text=self.read_file("index.html").replace("{{MEME_DATA}}", json.dumps(self.data)), content_type="text/html")
    async def h_up(self,r):
        rd = await r.multipart(); tag="未分类"
        while True:
            p = await rd.next()
            if not p: break
            if p.name == "file":
                raw_data = await p.read()
                loop = asyncio.get_running_loop()
                # 上传时也计算 hash
                current_hash = await loop.run_in_executor(self.executor, self.calc_dhash, raw_data)
                
                # 可选：上传时也去重？目前先不限制手动上传
                compressed_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, raw_data)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(compressed_data)
                self.data[fn] = {"tags": tag, "source": "manual"}
                if current_hash: self.img_hashes[fn] = current_hash
            elif p.name == "tags": tag = await p.text()
        self.save_data(); return web.Response(text="ok")
    
    async def h_slim(self, r):
        count = 0; total_saved = 0
        loop = asyncio.get_running_loop()
        print("[Meme] 开始批量瘦身 & 重建指纹...")
        self.img_hashes = {} # 清空重算
        for f in os.listdir(self.img_dir):
            path = os.path.join(self.img_dir, f)
            try:
                with open(path, 'rb') as file: raw = file.read()
                old_size = len(raw)
                
                # 重建指纹
                h = await loop.run_in_executor(self.executor, self.calc_dhash, raw)
                if h: self.img_hashes[f] = h

                # 压缩
                new_data, ext = await loop.run_in_executor(self.executor, self.compress_image_sync, raw)
                if len(new_data) < old_size:
                    with open(path, 'wb') as file: file.write(new_data)
                    count += 1; total_saved += (old_size - len(new_data))
            except: pass
        msg = f"已优化 {count} 张图片，指纹库已刷新"
        print(f"[Meme] {msg}")
        return web.Response(text=msg)

    async def h_del(self,r):
        for f in (await r.json()).get("filenames",[]):
            try: 
                os.remove(os.path.join(self.img_dir, f))
                del self.data[f]
                if f in self.img_hashes: del self.img_hashes[f]
            except: pass
        self.save_data(); return web.Response(text="ok")
    async def h_tag(self,r): d=await r.json(); self.data[d['filename']]['tags']=d['tags']; self.save_data(); return web.Response(text="ok")
    async def h_gcf(self,r): return web.json_response(self.local_config)
    async def h_ucf(self,r): 
        new_conf = await r.json()
        self.local_config.update(new_conf)
        self.save_config() 
        return web.Response(text="ok")
    async def h_backup(self, r):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(self.img_dir):
                for file in files: z.write(os.path.join(root, file), f"images/{file}")
            if os.path.exists(self.data_file): z.write(self.data_file, "memes.json")
            if os.path.exists(self.config_file): z.write(self.config_file, "config.json")
        buffer.seek(0)
        return web.Response(body=buffer, headers={'Content-Disposition': f'attachment; filename="meme_backup.zip"', 'Content-Type': 'application/zip'})
    async def h_restore(self, r):
        reader = await r.multipart()
        field = await reader.next()
        if not field or field.name != 'file': return web.Response(status=400, text="No file")
        buffer = io.BytesIO(await field.read())
        try:
            with zipfile.ZipFile(buffer, 'r') as z: z.extractall(self.base_dir)
            self.data = self.load_data(); self.local_config = self.load_config()
            # 恢复后必须重建指纹
            asyncio.create_task(self._init_image_hashes())
            return web.Response(text="ok")
        except Exception as e: return web.Response(status=500, text=str(e))

    def read_file(self, n): 
        with open(os.path.join(self.base_dir, n), "r", encoding="utf-8") as f: return f.read()
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
                        if chunk.strip(): buf.append(Plain(chunk))
                        flush(); chunk = ""
                    else: chunk += char
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        flush()
        return segs
    def find_best_match(self, query):
        best, score = None, 0
        for f, i in self.data.items():
            t = i.get("tags", "")
            if query in t: return os.path.join(self.img_dir, f)
            s = difflib.SequenceMatcher(None, query, t).ratio()
            if s > score: score = s; best = f
        if score > 0.4: return os.path.join(self.img_dir, best)
        return None
    def _get_img_url(self, e):
        for c in e.message_obj.message:
            if isinstance(c, Image): return c.url
        return None
    def load_config(self): return {**{"web_port":5000,"debounce_time":2.0,"reply_prob":50}, **(json.load(open(self.config_file)) if os.path.exists(self.config_file) else {})}
    def save_config(self): json.dump(self.local_config, open(self.config_file,"w"), indent=2)
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)
