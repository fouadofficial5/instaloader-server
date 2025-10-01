# -*- coding: utf-8 -*-
# FastAPI + (requests + instaloader كـ fallback): سرفر بسيط للتحقق وجلب صورة البروفايل

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, re, html, time

# instaloader اختياري (نستخدمه فقط إن توفر)
try:
    import instaloader
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
    HAS_INSTALOADER = True
except Exception:
    L = None
    HAS_INSTALOADER = False

app = FastAPI(title="Instaloader mini server", version="1.1")

# ===== CORS للسماح للتطبيق بالوصول =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Helpers =====
UA = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)

def _normalize(u: str) -> str:
    return u.strip().lstrip("@").replace(" ", "").lower()

# كاش بسيط بالذاكرة
_cache = {}
_CACHE_TTL = 60 * 10  # 10 دقائق

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

# ===== نماذج الرد =====
class ExistsResp(BaseModel):
    exists: bool
    reason: str  # ok | not_found | rate_limited | error

class PicResp(BaseModel):
    url: str

# ===== مسارات =====
@app.get("/")
def root():
    return {
        "ok": True,
        "message": "FastAPI Instagram helper",
        "endpoints": {
            "health": "/health",
            "exists": "/username/{username}/exists",
            "profile_pic": "/username/{username}/profile_pic",
            "docs": "/docs",
        },
    }

@app.get("/health")
def health():
    return {"ok": True}

# 1) التحقق من وجود اسم المستخدم
@app.get("/username/{username}/exists", response_model=ExistsResp)
def username_exists(username: str):
    username = _normalize(username)
    if not username or len(username) > 30:
        return ExistsResp(exists=False, reason="error")

    cache_key = f"exists:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # محاولة 1: طلب GET إلى صفحة انستغرام
    try:
        r = requests.get(
            f"https://www.instagram.com/{username}/",
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
        )
        if r.status_code in (200, 301, 302):
            data = ExistsResp(exists=True, reason="ok")
            _cache_set(cache_key, data)
            return data
        if r.status_code == 404:
            data = ExistsResp(exists=False, reason="not_found")
            _cache_set(cache_key, data)
            return data
        if r.status_code in (403, 429):
            # غالباً موجود لكن في Rate limit / Block
            data = ExistsResp(exists=True, reason="rate_limited")
            _cache_set(cache_key, data)
            return data
    except Exception:
        pass

    # محاولة 2: instaloader (إن وُجد)
    if HAS_INSTALOADER:
        try:
            instaloader.Profile.from_username(L.context, username)
            data = ExistsResp(exists=True, reason="ok")
            _cache_set(cache_key, data)
            return data
        except Exception:
            # إن قال لا يوجد أو حدث خطأ، نرجّح not_found
            data = ExistsResp(exists=False, reason="error")
            _cache_set(cache_key, data)
            return data

    return ExistsResp(exists=False, reason="error")

# 2) رابط صورة البروفايل (ندعم الشكلين بالـ underscore والـ dash)
def _get_profile_pic(username: str) -> str | None:
    username = _normalize(username)
    if not username or len(username) > 30:
        return None

    cache_key = f"pic:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.url if isinstance(cached, PicResp) else cached

    # أ) instaloader أولاً (إن وُجد)
    if HAS_INSTALOADER:
        try:
            profile = instaloader.Profile.from_username(L.context, username)
            url = str(profile.profile_pic_url)
            if url:
                data = PicResp(url=url)
                _cache_set(cache_key, data)
                return url
        except Exception:
            pass

    # ب) Scrape من HTML (hd ثم العادي)
    try:
        r = requests.get(
            f"https://www.instagram.com/{username}/",
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
        )
        html_text = r.text or ""

        m = re.search(r'"profile_pic_url_hd"\s*:\s*"([^"]+)"', html_text)
        if m:
            url = html.unescape(m.group(1)).replace("\\u0026", "&").replace("\\/", "/")
            data = PicResp(url=url)
            _cache_set(cache_key, data)
            return url

        m2 = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', html_text)
        if m2:
            url = html.unescape(m2.group(1)).replace("\\u0026", "&").replace("\\/", "/")
            data = PicResp(url=url)
            _cache_set(cache_key, data)
            return url
    except Exception:
        pass

    return None

@app.get("/username/{username}/profile_pic", response_model=PicResp)
def profile_pic(username: str):
    url = _get_profile_pic(username)
    if not url:
        raise HTTPException(status_code=404, detail="not found")
    return PicResp(url=url)

# نفس المسار لكن بـ dash للمتوافقية
@app.get("/username/{username}/profile-pic", response_model=PicResp)
def profile_pic_dash(username: str):
    url = _get_profile_pic(username)
    if not url:
        raise HTTPException(status_code=404, detail="not found")
    return PicResp(url=url)
