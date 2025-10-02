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

# ====== NEW: تحقق المتابعة + منح العملات عبر Firebase ======
import os, json
from pydantic import BaseModel
from typing import Optional

# Firebase Admin
FIREBASE_INITIALIZED = False
db_admin = None

def _init_firebase_once():
    global FIREBASE_INITIALIZED, db_admin
    if FIREBASE_INITIALIZED:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        # نقرأ JSON لمفاتيح الخدمة من متغير بيئة FIREBASE_CREDENTIALS_JSON
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

# تسجيل الدخول لـ Instaloader (حساب checker) مرة واحدة
IG_LOGGED_IN = False
def _ensure_ig_login():
    global IG_LOGGED_IN
    if IG_LOGGED_IN or not HAS_INSTALOADER:
        return
    ig_user = os.environ.get("IG_CHECKER_USER", "")
    ig_pass = os.environ.get("IG_CHECKER_PASS", "")
    if not ig_user or not ig_pass:
        # لو ما في حساب checker، هنكمل بالـ scraping (أقل دقة)
        return
    try:
        L.login(ig_user, ig_pass)
        IG_LOGGED_IN = True
    except Exception as e:
        print("Instaloader login failed:", e)

class VerifyReq(BaseModel):
    taskId: str
    claimant: str   # الذي يدعي أنه تابع
    target: str     # المطلوب متابعته (صاحب المهمة)

class VerifyResp(BaseModel):
    ok: bool
    reason: Optional[str] = None
    newCoins: Optional[int] = None    # رصيد المُطالب بعد المكافأة، إن وُجد

def _user_follows_target(claimant: str, target: str) -> bool:
    """
    يرجع True فقط إذا تأكدنا أن claimant يتابع target.
    نفضّل استخدام instaloader مع تسجيل دخول checker (أدق)،
    وإلا نسقط على Scrape (أضعف وقد يفشل مع الحسابات الخاصة).
    """
    claimant = _normalize(claimant)
    target = _normalize(target)

    # A) الأفضل: Instaloader مع تسجيل دخول
    if HAS_INSTALOADER:
        _ensure_ig_login()
        try:
            target_prof = instaloader.Profile.from_username(L.context, target)
            # تحذير: استعراض كل المتابعين قد يكون بطيء؛ نحده بعدد معقول
            # نبحث بالاسم مباشرة (case-insensitive)
            limit = int(os.environ.get("IG_FOLLOWERS_SCAN_LIMIT", "1000"))
            count = 0
            for follower in target_prof.get_followers():
                if follower.username.lower() == claimant:
                    return True
                count += 1
                if count >= limit:
                    break
        except Exception as e:
            print("Instaloader check failed:", e)

    # B) fallback ضعيف: لا يوجد طريقة موثوقة بدون تسجيل دخول
    # هنرجع False لتجنّب الغش (أو يمكنك إرجاع None ويعني "غير قادر على التحقق")
    return False

def _award_and_close_task(db, task_id: str, claimant: str, reward_coins: int = 10) -> int:
    """
    يُضيف عملات للمطالب claimant إن لم يكن أخذها من قبل لهذه المهمة،
    ويحدّث progress للمهمة/الطلب.
    يرجع الرصيد الجديد للمطالب بعد الإضافة.
    """
    import google.cloud.firestore
    from google.cloud.firestore_v1 import Transaction

    claimant = _normalize(claimant)

    @google.cloud.firestore.transactional
    def _tx(transaction: Transaction):
        task_ref = db.collection("followTasks").document(task_id)
        task_snap = task_ref.get(transaction=transaction)
        if not task_snap.exists:
            raise RuntimeError("TASK_NOT_FOUND")

        active = task_snap.get("active") is True
        need   = int(task_snap.get("need") or 0)
        done   = int(task_snap.get("doneCount") or 0)
        order_id = task_snap.get("orderId") or ""
        target_user = task_snap.get("targetUsername") or ""
        if not active:
            raise RuntimeError("TASK_NOT_ACTIVE")

        # participants/<claimant>
        part_ref = task_ref.collection("participants").document(claimant)
        part_snap = part_ref.get(transaction=transaction)
        if not part_snap.exists:
            # لازم يكون طالب “Start” من قبل (claimFollowStart) — لو حاب تتساهل احذف هذا الشرط
            raise RuntimeError("NO_PARTICIPATION")

        if part_snap.get("followed") is True:
            # سبق وأخذها
            user_ref = db.collection("users").document(claimant)
            user_snap = user_ref.get(transaction=transaction)
            return int(user_snap.get("coins") or 0)

        # علّم أنه اتبع
        transaction.update(part_ref, {
            "followed": True,
            "confirmedAt": google.cloud.firestore.SERVER_TIMESTAMP
        })

        # زد العداد
        new_done = done + 1
        transaction.update(task_ref, {"doneCount": new_done})

        # حدّث الطلب
        if order_id and target_user:
            order_ref = db.collection("users").document(target_user).collection("orders").document(order_id)
            transaction.update(order_ref, {"doneCount": new_done})
            if new_done >= need:
                transaction.update(task_ref, {"active": False})
                transaction.update(order_ref, {"status": "DONE"})

        # أضف مكافأة العملات للمُطالب
        user_ref = db.collection("users").document(claimant)
        user_snap = user_ref.get(transaction=transaction)
        if not user_snap.exists:
            transaction.set(user_ref, {
                "username": claimant,
                "coins": reward_coins,
                "createdAt": int(time.time() * 1000),
                "profilePicUrl": ""
            })
            return reward_coins
        else:
            cur = int(user_snap.get("coins") or 0)
            newv = cur + reward_coins
            transaction.update(user_ref, {"coins": newv, "username": claimant})
            return newv

    tx = db.transaction()
    new_coins = _tx(tx)
    return new_coins

@app.post("/verify/and_award", response_model=VerifyResp)
def verify_and_award(req: VerifyReq):
    """
    يتأكد إذا claimant يتابع target.
    - إن تأكدنا: نُحدّث Firestore (participants/task/order) ونمنح claimant 10 عملات.
    - إن لم نتأكد: نرجع ok=false.
    """
    _init_firebase_once()

    claimant = _normalize(req.claimant)
    target   = _normalize(req.target)
    if not claimant or not target or not req.taskId:
        return VerifyResp(ok=False, reason="bad_request")

    # التحقق
    followed = _user_follows_target(claimant, target)
    if not followed:
        return VerifyResp(ok=False, reason="not_following")

    # منح العملات وتحديث المهمة
    try:
        new_coins = _award_and_close_task(db_admin, req.taskId, claimant, reward_coins=int(os.environ.get("FOLLOW_REWARD_COINS","10")))
        return VerifyResp(ok=True, newCoins=new_coins)
    except Exception as e:
        print("award failed:", e)
        return VerifyResp(ok=False, reason="award_failed")

# ===== تحقق متابعة: هل source يتابع target؟ =====
from fastapi import Query
import os

IG_LOGIN = os.getenv("IG_LOGIN")       # موجودة عندك في Render
IG_PASSWORD = os.getenv("IG_PASSWORD") # موجودة عندك في Render

def _ensure_login():
    global L, HAS_INSTALOADER
    if not HAS_INSTALOADER:
        return False
    try:
        if not getattr(L.context, "_logged_in", False):
            L.login(IG_LOGIN, IG_PASSWORD)
            L.context._logged_in = True
        return True
    except Exception:
        return False

@app.get("/verify_follow")
def verify_follow(
    source: str = Query(..., description="username العامل"),
    target: str = Query(..., description="username الهدف")
):
    source = _normalize(source)
    target = _normalize(target)
    if not source or not target or len(source) > 30 or len(target) > 30:
        return {"follows": False, "reason": "invalid"}

    if not _ensure_login():
        return {"follows": False, "reason": "login_failed"}

    try:
        # نحمل متابَعات المصدر ونبحث عن الهدف
        src = instaloader.Profile.from_username(L.context, source)
        for f in src.get_followees():
            if f.username.lower() == target:
                return {"follows": True, "reason": "ok"}
        return {"follows": False, "reason": "not_following"}
    except Exception as e:
        return {"follows": False, "reason": "error"}
