# -*- coding: utf-8 -*-
# FastAPI + (requests + instaloader كـ fallback): سرفر بسيط للتحقق وجلب صورة البروفايل

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, re, html, time, os, json
from typing import Optional

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

# 1) التحقق من وجود اسم المستخدم (أكثر دقة)
@app.get("/username/{username}/exists", response_model=ExistsResp)
def username_exists(username: str):
    username = _normalize(username)
    if not username or len(username) > 30:
        return ExistsResp(exists=False, reason="error")

    cache_key = f"exists:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"https://www.instagram.com/{username}/?hl=en",
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
            allow_redirects=True,
        )
        html_text = r.text or ""
        code = r.status_code

        if code == 404:
            data = ExistsResp(exists=False, reason="not_found"); _cache_set(cache_key, data); return data

        if code == 200:
            if any(m in html_text.lower() for m in ["page not found","the link you followed may be broken","sorry, this page isn't available"]):
                data = ExistsResp(exists=False, reason="not_found"); _cache_set(cache_key, data); return data
            if any(m in html_text for m in ['"profile_pic_url"','"profile_pic_url_hd"','profilePage_','"is_private"','"edge_followed_by"']):
                data = ExistsResp(exists=True, reason="ok"); _cache_set(cache_key, data); return data

        # 403/429 → جرّب instaloader
        if HAS_INSTALOADER:
            try:
                instaloader.Profile.from_username(L.context, username)
                data = ExistsResp(exists=True, reason="ok"); _cache_set(cache_key, data); return data
            except Exception:
                data = ExistsResp(exists=False, reason="not_found"); _cache_set(cache_key, data); return data

        reason = "rate_limited" if code in (403,429) else "error"
        data = ExistsResp(exists=False, reason=reason); _cache_set(cache_key, data); return data

    except Exception:
        if HAS_INSTALOADER:
            try:
                instaloader.Profile.from_username(L.context, username)
                data = ExistsResp(exists=True, reason="ok"); _cache_set(cache_key, data); return data
            except Exception: pass
        return ExistsResp(exists=False, reason="error")
# 2) رابط صورة البروفايل
def _get_profile_pic(username: str) -> str | None:
    username = _normalize(username)
    if not username or len(username) > 30:
        return None

    cache_key = f"pic:{username}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.url if isinstance(cached, PicResp) else cached

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

@app.get("/username/{username}/profile-pic", response_model=PicResp)
def profile_pic_dash(username: str):
    url = _get_profile_pic(username)
    if not url:
        raise HTTPException(status_code=404, detail="not found")
    return PicResp(url=url)
# ===== Firebase =====
FIREBASE_INITIALIZED = False
db_admin = None

def _init_firebase_once():
    global FIREBASE_INITIALIZED, db_admin
    if FIREBASE_INITIALIZED:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
        if not creds_json:
            raise RuntimeError("FIREBASE_CREDENTIALS_JSON env not set")
        cred = firebase_admin.credentials.Certificate(json.loads(creds_json))
        firebase_admin.initialize_app(cred)
        db_admin = firestore.client()
        FIREBASE_INITIALIZED = True
    except Exception as e:
        print("Firebase init failed:", e)
        raise

# ===== تحميل جلسة إنستغرام من ملف (أفضل من الباسورد) =====
IG_SESSION_FILE = os.environ.get("IG_SESSION_FILE") or os.environ.get("SESSION_FILE")
IG_SESSION_LOADED = False
if HAS_INSTALOADER and IG_SESSION_FILE:
    try:
        L.load_session_from_file(None, IG_SESSION_FILE)
        IG_SESSION_LOADED = True
        print(f"[IG] Session loaded from {IG_SESSION_FILE}")
    except Exception as e:
        print(f"[IG] Failed to load session from file: {e}")

# ===== تسجيل الدخول =====
IG_LOGIN = os.getenv("IG_LOGIN")
IG_PASSWORD = os.getenv("IG_PASSWORD")

def _ensure_login():
    """
    يضمن أن L مسجّل دخول.
    يحاول أولاً تحميل الجلسة، وإن لم ينجح يجرب IG_LOGIN/IG_PASSWORD.
    """
    global L, HAS_INSTALOADER, IG_SESSION_LOADED
    if not HAS_INSTALOADER:
        return False
    if IG_SESSION_LOADED:
        return True
    if IG_LOGIN and IG_PASSWORD:
        try:
            L.login(IG_LOGIN, IG_PASSWORD)
            IG_SESSION_LOADED = True
            return True
        except Exception as e:
            print("Instaloader login failed:", e)
    return False

# ===== تحقق المتابعة =====
class VerifyReq(BaseModel):
    taskId: str
    claimant: str
    target: str

class VerifyResp(BaseModel):
    ok: bool
    reason: Optional[str] = None
    newCoins: Optional[int] = None

@app.get("/verify_follow")
def verify_follow(source: str = Query(...), target: str = Query(...)):
    source = _normalize(source)
    target = _normalize(target)
    if not source or not target or len(source) > 30 or len(target) > 30:
        return {"follows": False, "reason": "invalid"}
    if not _ensure_login():
        return {"follows": False, "reason": "login_failed"}
    try:
        src = instaloader.Profile.from_username(L.context, source)
        for f in src.get_followees():
            if f.username.lower() == target:
                return {"follows": True, "reason": "ok"}
        return {"follows": False, "reason": "not_following"}
    except Exception:
        return {"follows": False, "reason": "error"}
