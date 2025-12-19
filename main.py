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
from aiohttp import web

from PIL import Image as PILImage

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print("DEBUG: MemeMaster Pro (Final Fix) 已加载")

@register("vv_meme_master", "MemeMaster", "防抖+表情包+拟人分段", "1.0.6")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        self.data = self.load_data()
        
        self.sessions = {}
        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '[': ']', '{': '}'}

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
        except Exception as e:
            print(f"ERROR: Web后台启动失败: {e}")

    # ==========================
    # 图片压缩与处理
    # ==========================
    def compress_image(self, image_data: bytes) -> tuple[bytes, str]:
        try:
            img = PILImage.open(io.BytesIO(image_data))
            max_width = 800
            if img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), PILImage.Resampling.LANCZOS)
            
            buffer = io.BytesIO()
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue(), ".png"
            else:
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buffer, format="JPEG", quality=80, optimize=True)
                return buffer.getvalue(), ".jpg"
        except Exception as e:
            print(f"图片处理失败: {e}，将保存原图")
            return image_data, ".jpg" 

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

            # 暗线：自动进货
            if img_url and not msg_str and not msg_str.startswith("/"):
                cooldown = self.local_config.get("auto_save_cooldown", 60)
                last_save = getattr(self, "last_auto_save_time", 0)
                if time.time() - last_save > cooldown:
                    print(f"[Meme] 暗线启动：正在后台鉴定图片...")
                    asyncio.create_task(self.ai_evaluate_image(img_url))

            # 指令穿透
            if msg_str.startswith("/") or msg_str.startswith("！") or msg_str.startswith("!"):
                if uid in self.sessions:
                    if self.sessions[uid].get('timer_task'): self.sessions[uid]['timer_task'].cancel()
                    self.sessions[uid]['flush_event'].set()
                return

            # 防抖逻辑
            debounce_time = self.local_config.get("debounce_time", 2.0)
            if debounce_time <= 0: return

            if uid in self.sessions:
                s = self.sessions[uid]
                if msg_str: s['buffer'].append(msg_str)
                if img_url: s['images'].append(img_url)
                if s.get('timer_task'): s['timer_task'].cancel()
                s['timer_task'] = asyncio.create_task(self._timer_coroutine(uid, debounce_time))
                event.stop_event()
                return

            flush_event = asyncio.Event()
            timer_task = asyncio.create_task(self._timer_coroutine(uid, debounce_time))
            self.sessions[uid] = {
                'buffer': [msg_str] if msg_str else [],
                'images': [img_url] if img_url else [],
                'flush_event': flush_event,
                'timer_task': timer_task
            }
            # Log: 开始防抖
            # print(f"[Meme] 收到消息，开始防抖 ({debounce_time}s)...")
            await flush_event.wait()

            # 结算
            if uid not in self.sessions: return
            s = self.sessions.pop(uid)
            merged_text = "\n".join(s['buffer']).strip()
            
            # Log: 防抖结束
            print(f"[Meme] 防抖结算: {len(s['buffer'])} 条文本, {len(s['images'])} 张图 -> 发送给LLM")

            if not merged_text and not s['images']: return

            # 注入小抄
            if merged_text and random.randint(1, 100) <= self.local_config.get("reply_prob", 50):
                all_tags = [i.get("tags") for i in self.data.values()]
                if all_tags:
                    hint_tags = "、".join(random.sample(all_tags, min(20, len(all_tags))))
                    merged_text += f"\n\n[System]\nAvailable Memes: {hint_tags}\nTo use, reply: MEME_TAG:tag_name"

            # 图片透传
            new_chain = []
            if merged_text: new_chain.append(Plain(merged_text))
            for url in s['images']: new_chain.append(Image.fromURL(url))
            
            event.message_str = merged_text
            event.message_obj.message = new_chain
            
        except Exception as e:
            print(f"ERROR inside handler: {e}")
            return

    # ==========================
    # 核心 2: 输出端 (正则修复 + 日志增强)
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
        
        # Log: AI 原始回复
        print(f"[Meme] AI原始回复: {text[:50]}..." if len(text) > 50 else f"[Meme] AI原始回复: {text}")

        try:
            # 修复正则：匹配 MEME_TAG: 及其后的所有非空白字符（包括中文冒号、斜杠等）
            # \s* 允许 MEME_TAG: 后有空格
            # [\S]+ 匹配所有非空白字符
            parts = re.split(r"(MEME_TAG:\s*[\S]+)", text)
            
            mixed_chain = []
            has_tag = False
            
            for part in parts:
                if "MEME_TAG:" in part:
                    tag = part.replace("MEME_TAG:", "").strip()
                    path = self.find_best_match(tag)
                    if path: 
                        print(f"🎯 命中图片: {tag}")
                        mixed_chain.append(Image.fromFileSystem(path))
                        has_tag = True
                    else:
                        print(f"⚠️ 未找到图片: {tag}")
                        # 找不到图，选择忽略，保持对话流畅
                        pass 
                elif part:
                    mixed_chain.append(Plain(part))
            
            if not has_tag and len(text) < 100 and "。" not in text: 
                return 

            segments = self.smart_split(mixed_chain)
            print(f"[Meme] 智能分段: 共 {len(segments)} 段")
            
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_content = "".join([c.text for c in seg if isinstance(c, Plain)])
                img_count = sum(1 for c in seg if isinstance(c, Image))
                
                # 计算延迟
                txt_len = len(txt_content)
                wait = delay_base + (txt_len * delay_factor)
                
                # Log: 发送内容
                log_msg = txt_content
                if img_count > 0: log_msg += f" [图片*{img_count}]"
                print(f"--> 发送 (段 {i+1}): {log_msg}")

                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                
                if i < len(segments) - 1:
                    # print(f"... 拟人延迟 {wait:.1f}s ...")
                    await asyncio.sleep(wait)
            
            event.set_result(None)

        except Exception as e:
            print(f"分段发送出错: {e}")

    # ==========================
    # 核心 3: 暗线 - 自动进货
    # ==========================
    async def ai_evaluate_image(self, img_url):
        try:
            self.last_auto_save_time = time.time()
            provider = self.context.get_using_provider()
            if not provider: return
            
            prompt = """你正在帮我整理一个 QQ 表情包素材库。
请判断这张图片是否“值得被保存”，
作为未来聊天中可能会使用的表情包素材。
配文是：“{context_text}”。
判断时请注意：
- 这是一个偏二次元 / meme 使用环境
- 常见来源包括：chiikawa、这狗、线条小狗、多栋、猫meme 等
- 不要过度严肃，也不要把普通照片当成表情包
如果这张图不适合做表情包，请只回复：
NO
如果适合，请严格按下面格式回复（不要多余内容）：
YES
<名称>:<一句自然语言解释这个表情包在什么语境下使用>
规则：
1. 如果你能明确判断这是某个常见 IP、角色或 meme 系列，
   请直接使用大家普遍认得的名字作为「名称」
   例如：chiikawa、这狗、线条小狗、多栋、猫meme
2. 如果无法确定具体 IP，不要强行猜测，
   请使用一个简短的情绪或语气概括作为「名称」
3. 冒号后必须是一句完整、自然的“使用说明”，
   描述人在什么情况下会用这个表情包"""

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
                    await self._save_img(img_url, full_tag, "auto")
        except Exception as e:
            print(f"鉴图出错: {e}")

    # ==========================
    # 辅助工具
    # ==========================
    def smart_split(self, chain):
        segs = []; buf = []
        def flush():
            if buf: segs.append(buf[:]); buf.clear()
        for c in chain:
            if isinstance(c, Image):
                flush(); segs.append([c]); continue
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

    # ==========================
    # Web Server
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
                compressed_data, ext = self.compress_image(raw_data)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: 
                    f.write(compressed_data)
                self.data[fn] = {"tags": tag, "source": "manual"}
            elif p.name == "tags": tag = await p.text()
        self.save_data(); return web.Response(text="ok")
        
    async def h_del(self,r):
        for f in (await r.json()).get("filenames",[]):
            try: os.remove(os.path.join(self.img_dir, f)); del self.data[f]
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
        return web.Response(body=buffer, headers={'Content-Disposition': f'attachment; filename="meme_backup_{int(time.time())}.zip"', 'Content-Type': 'application/zip'})

    async def h_restore(self, r):
        reader = await r.multipart()
        field = await reader.next()
        if not field or field.name != 'file': return web.Response(status=400, text="No file")
        buffer = io.BytesIO(await field.read())
        try:
            with zipfile.ZipFile(buffer, 'r') as z:
                z.extractall(self.base_dir)
            self.data = self.load_data(); self.local_config = self.load_config()
            return web.Response(text="ok")
        except Exception as e: return web.Response(status=500, text=str(e))

    def read_file(self, n): 
        with open(os.path.join(self.base_dir, n), "r", encoding="utf-8") as f: return f.read()
    
    async def _save_img(self, url, tag, src):
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                raw_data = await r.read()
                compressed_data, ext = self.compress_image(raw_data)
                fn = f"{int(time.time())}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: 
                    f.write(compressed_data)
                self.data[fn] = {"tags": tag, "source": src}; self.save_data()
                
    def _get_img_url(self, e):
        for c in e.message_obj.message:
            if isinstance(c, Image): return c.url
        return None
    def load_config(self): return {**{"web_port":5000,"debounce_time":2.0,"reply_prob":50}, **(json.load(open(self.config_file)) if os.path.exists(self.config_file) else {})}
    def save_config(self): json.dump(self.local_config, open(self.config_file,"w"), indent=2)
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)
