# -*- coding: utf-8 -*-
# FastAPI + Instaloader: سرفر بسيط لجلب صورة البروفايل والتحقق من وجود الاسم

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import instaloader
import requests
import re
import html
import time

app = FastAPI(title="Instaloader mini server", version="1.0")

# السماح للواجهة (أندرويد) بالوصول مباشرة
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== إعداد Instaloader (بدون تسجيل دخول) =====
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
    post_metadata_txt_pattern="",
    compress_json=False,
    max_connection_attempts=1,
    user_agent=(
        "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
)

# كاش بسيط في الذاكرة لتخفيف الطلبات
_cache = {}
_CACHE_TTL = 60 * 10  # عشر دقائق

def _cache_get(key):
    v = _cache.get(key)
    if not v:
        return None
    if time.time() - v["at"] > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return v["data"]

def _cache_set(key, data):
    _cache[key] = {"data": data, "at": time.time()}

UA = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)

def _normalize(u: str) -> str:
    return u.strip().lstrip("@").replace(" ", "").lower()

# ===== نماذج ردود =====
class ExistsResp(BaseModel):
    exists: bool
    reason: str  # ok | not_found | rate_limited | error

class PicResp(BaseModel):
    url: str

@app.get("/health")
def health():
    return {"ok": True}

# ========= 1) التحقق من وجود اسم المستخدم =========
@app.get("/username/{username}/exists", response_model=ExistsResp)
def username_exists(username: str):
    username = _normalize(username)
    if not username or len(username) > 30:
        return ExistsResp(exists=False, reason="error")

    # كاش
    cache_key = f"exists:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # محاولة 1: HTTP رأسية إلى صفحة انستغرام
    try:
        r = requests.get(f"https://www.instagram.com/{username}/", headers={"User-Agent": UA}, timeout=10)
        if r.status_code in (200, 301, 302):
            data = ExistsResp(exists=True, reason="ok")
            _cache_set(cache_key, data)
            return data
        if r.status_code == 404:
            data = ExistsResp(exists=False, reason="not_found")
            _cache_set(cache_key, data)
            return data
        if r.status_code in (403, 429):
            # مع ذلك نعتبره موجود غالباً لكن نميّز السبب
            data = ExistsResp(exists=True, reason="rate_limited")
            _cache_set(cache_key, data)
            return data
    except Exception:
        pass

    # محاولة 2: Instaloader (قد يفشل عند الـ rate limit)
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        data = ExistsResp(exists=True, reason="ok")
        _cache_set(cache_key, data)
        return data
    except instaloader.exceptions.ProfileNotExistsException:
        data = ExistsResp(exists=False, reason="not_found")
        _cache_set(cache_key, data)
        return data
    except Exception:
        return ExistsResp(exists=False, reason="error")

# ========= 2) رابط صورة البروفايل =========
@app.get("/username/{username}/profile_pic", response_model=PicResp)
def profile_pic(username: str):
    username = _normalize(username)
    if not username or len(username) > 30:
        raise HTTPException(status_code=400, detail="invalid username")

    # كاش
    cache_key = f"pic:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # محاولة 1: Instaloader (الأفضل والدقة العالية)
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        url = profile.profile_pic_url
        if url:
            data = PicResp(url=str(url))
            _cache_set(cache_key, data)
            return data
    except Exception:
        pass

    # محاولة 2: سكراب بسيط من HTML (hd أولاً ثم العادي)
    try:
        r = requests.get(f"https://www.instagram.com/{username}/", headers={"User-Agent": UA}, timeout=10)
        html_text = r.text or ""

        # profile_pic_url_hd
        m = re.search(r'"profile_pic_url_hd"\s*:\s*"([^"]+)"', html_text)
        if m:
            url = html.unescape(m.group(1)).replace("\\u0026", "&").replace("\\/", "/")
            data = PicResp(url=url)
            _cache_set(cache_key, data)
            return data

        # profile_pic_url
        m2 = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', html_text)
        if m2:
            url = html.unescape(m2.group(1)).replace("\\u0026", "&").replace("\\/", "/")
            data = PicResp(url=url)
            _cache_set(cache_key, data)
            return data
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="not found")
