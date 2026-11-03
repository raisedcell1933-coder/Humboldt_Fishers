from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash, send_from_directory, abort, g
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import re
import urllib.request
from threading import Lock
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3, os, secrets, mimetypes, json
from authlib.integrations.flask_client import OAuth

app = Flask(__name__)
app.secret_key = os.environ.get("HUMBOLDT_FISHERS_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
@app.context_processor
def inject_google_account():
    return {"current_google_user": session.get("google_user")}

@app.after_request
def preload_humboldt_water_data(response):
    """Warm the browser's shared water-feature cache on every non-map page."""
    if request.path == "/map" or "text/html" not in response.headers.get("Content-Type", ""):
        return response
    preload_script = r"""
<script>
(function(){
  var key='fishHumboldtWaterwaysGeoJSON_v1';
  try {
    var cached=localStorage.getItem(key);
    if(cached){var parsed=JSON.parse(cached);if(parsed&&Array.isArray(parsed.elements)&&parsed.elements.length)return;}
  } catch(e) {}
  if(window.__hfWaterPreloadStarted)return;
  window.__hfWaterPreloadStarted=true;
  var bbox='40.0,-124.55,-123.35,41.55';
  /* Overpass uses south,west,north,east; keep the same bounds as the map. */
  bbox='40.0,-124.55,41.55,-123.35';
  var query='[out:json][timeout:25];('+
    'way["waterway"~"^(river|stream|canal|tidal_channel)$"]["name"]('+bbox+');'+
    'way["natural"~"^(water|bay)$"]["name"]('+bbox+');'+
    'way["water"~"^(lake|lagoon|bay)$"]["name"]('+bbox+');'+
    ');out geom qt;';
  var hosts=['https://overpass-api.de/api/interpreter','https://overpass.kumi.systems/api/interpreter'];
  function get(i){
    return fetch(hosts[i],{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:'data='+encodeURIComponent(query)})
      .then(function(r){if(!r.ok)throw new Error('Water data unavailable');return r.json();})
      .then(function(data){if(data&&Array.isArray(data.elements)&&data.elements.length){try{localStorage.setItem(key,JSON.stringify(data));}catch(e){}}return data;})
      .catch(function(err){if(i+1<hosts.length)return get(i+1);throw err;});
  }
  get(0).catch(function(){});
})();
</script>
"""
    try:
        html=response.get_data(as_text=True)
        if "</body>" in html:
            response.set_data(html.replace("</body>",preload_script+"</body>",1))
    except Exception:
        pass
    return response

ADMIN_USERNAME = os.environ.get("HUMBOLDT_FISHERS_ADMIN_USER", "admin")
# For public hosting, set HUMBOLDT_FISHERS_ADMIN_PASSWORD to a strong secret in host settings.
ADMIN_PASSWORD = os.environ.get("HUMBOLDT_FISHERS_ADMIN_PASSWORD", "")
BASE_DIR = Path(__file__).resolve().parent
# Render's filesystem is temporary unless a persistent disk is mounted.
# Set HUMBOLDT_FISHERS_DATA_DIR to that disk's mount path (commonly /var/data)
# so story submissions, moderation status, and uploaded photos survive restarts.
DATA_DIR = Path(os.environ.get("HUMBOLDT_FISHERS_DATA_DIR", str(BASE_DIR))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(BASE_DIR / 'static', 'favicon.svg', mimetype='image/svg+xml')
UPLOAD_DIR = DATA_DIR / "private_uploads"
DB_PATH = DATA_DIR / "photo_submissions.sqlite3"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
UPLOAD_DIR.mkdir(exist_ok=True)

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Story moderation data is stored in Neon when DATABASE_URL is configured.
# Other site features continue using the existing local SQLite database.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

def story_db_connect():
    if not DATABASE_URL:
        return db_connect()
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_story_db():
    if not DATABASE_URL:
        return
    with story_db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS fishing_stories (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            filename TEXT,
            original_name TEXT,
            image_url TEXT,
            cloudinary_public_id TEXT,
            status_token TEXT NOT NULL UNIQUE,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )""")
        conn.execute("ALTER TABLE fishing_stories ADD COLUMN IF NOT EXISTS image_url TEXT")
        conn.execute("ALTER TABLE fishing_stories ADD COLUMN IF NOT EXISTS cloudinary_public_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fishing_stories_status_id ON fishing_stories(status, id DESC)")

init_story_db()

def init_photo_db():
    with db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            species TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fishing_stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            filename TEXT,
            original_name TEXT,
            image_url TEXT,
            cloudinary_public_id TEXT,
            status_token TEXT NOT NULL UNIQUE,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )""")
        story_columns = {row[1] for row in conn.execute("PRAGMA table_info(fishing_stories)").fetchall()}
        if "image_url" not in story_columns:
            conn.execute("ALTER TABLE fishing_stories ADD COLUMN image_url TEXT")
        if "cloudinary_public_id" not in story_columns:
            conn.execute("ALTER TABLE fishing_stories ADD COLUMN cloudinary_public_id TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            viewed_at REAL NOT NULL,
            path TEXT NOT NULL,
            visitor_id TEXT
        )""")
        # Upgrade an existing database created by an older version.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(page_views)").fetchall()}
        if "visitor_id" not in columns:
            conn.execute("ALTER TABLE page_views ADD COLUMN visitor_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_viewed_at ON page_views(viewed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_visitor_time ON page_views(visitor_id, viewed_at)")

init_photo_db()
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_BYTES + 256 * 1024

# A long-lived first-party cookie recognizes the same browser/device between page loads.
# This is a privacy-friendly approximation, not a guaranteed physical-device identity.
@app.before_request
def identify_and_record_public_visit():
    visitor_id = request.cookies.get("hf_device_id")
    if not visitor_id or len(visitor_id) > 100:
        visitor_id = secrets.token_urlsafe(24)
        g.new_visitor_id = visitor_id
    g.analytics_visitor_id = visitor_id

    # Count public page loads, but dashboard totals below count distinct browser IDs.
    if request.method != "GET":
        return
    path = request.path
    if path.startswith(("/admin", "/api/", "/static/", "/community-photo-file/", "/admin-private-photo/", "/fishing-story-image/")):
        return
    if path == "/fishing-story-status" or path.startswith("/fishing-story-status/"):
        return
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO page_views(viewed_at, path, visitor_id) VALUES (?, ?, ?)", (datetime.now().timestamp(), path, visitor_id))
    except sqlite3.Error:
        # Analytics must never stop the public site from loading.
        pass

@app.after_request
def set_device_recognition_cookie(response):
    visitor_id = getattr(g, "new_visitor_id", None)
    if visitor_id:
        response.set_cookie("hf_device_id", visitor_id, max_age=365 * 24 * 60 * 60,
                            httponly=True, samesite="Lax", secure=request.is_secure, path="/")
    return response

def admin_required():
    return bool(session.get("is_admin"))

def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def cloudinary_enabled():
    return bool(os.environ.get("CLOUDINARY_URL", "").strip())

def upload_story_image(data):
    """Upload a validated story image to Cloudinary and return its durable URL and ID."""
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(cloudinary_url=os.environ["CLOUDINARY_URL"].strip())
    result = cloudinary.uploader.upload(
        data,
        folder="fish-humboldt/stories",
        resource_type="image",
        unique_filename=True,
        overwrite=False,
    )
    return result["secure_url"], result["public_id"]

def delete_cloudinary_image(public_id):
    if not public_id or not cloudinary_enabled():
        return
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloudinary_url=os.environ["CLOUDINARY_URL"].strip())
        cloudinary.uploader.destroy(public_id, resource_type="image")
    except Exception:
        # Story deletion should still complete if the image provider is temporarily unavailable.
        app.logger.exception("Could not delete Cloudinary story image %s", public_id)

NOAA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATION = "9418767"  # North Spit, Humboldt Bay, CA
PACIFIC = ZoneInfo("America/Los_Angeles")
CACHE_SECONDS = 900
cache = {"fetched": None, "data": None}
wind_cache = {"fetched": None, "data": None}
weather_cache = {"fetched": None, "data": None}
lock = Lock()

# Approximate town-center coordinates in Humboldt County, California.
WEATHER_TOWNS = {
    "Eureka": (40.8021, -124.1637),
    "Fortuna": (40.5982, -124.1573),
    "Arcata": (40.8665, -124.0828),
    "McKinleyville": (40.9465, -124.1006),
    "Rio Dell": (40.4993, -124.1064),
    "Scotia": (40.4829, -124.1045),
}

def fetch_nws_hourly(lat, lon):
    """Fetch a real 16-day hourly weather forecast from Open-Meteo for the selected town."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": "America/Los_Angeles", "forecast_days": 16,
    }
    response = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=25)
    response.raise_for_status()
    payload = response.json()
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    codes = hourly.get("weather_code", [])
    def description(code):
        try: code = int(code)
        except (TypeError, ValueError): return "Forecast unavailable"
        if code == 0: return "Clear sky"
        if code in (1, 2): return "Mainly clear" if code == 1 else "Partly cloudy"
        if code == 3: return "Overcast"
        if code in (45, 48): return "Fog"
        if code in (51, 53, 55, 56, 57): return "Drizzle"
        if code in (61, 63, 65, 66, 67, 80, 81, 82): return "Rain showers" if code >= 80 else "Rain"
        if code in (71, 73, 75, 77, 85, 86): return "Snow"
        if code in (95, 96, 99): return "Thunderstorms"
        return "Mixed conditions"
    periods = []
    for i, stamp in enumerate(times):
        periods.append({
            "startTime": stamp,
            "temperature": (hourly.get("temperature_2m") or [None] * len(times))[i],
            "temperatureUnit": "F",
            "weatherCode": codes[i] if i < len(codes) else None,
            "cloudCover": (hourly.get("cloud_cover") or [None] * len(times))[i],
            "shortForecast": description(codes[i] if i < len(codes) else None),
            "precipitationProbability": (hourly.get("precipitation_probability") or [None] * len(times))[i],
            "windSpeed": (hourly.get("wind_speed_10m") or [None] * len(times))[i],
            "windDirection": (hourly.get("wind_direction_10m") or [None] * len(times))[i],
            "icon": None,
        })
    return periods

WEATHER_HISTORY_PATH = BASE_DIR / "weather_weekly_cache.json"

def _week_bounds(now):
    """Current Monday 00:00 through next Monday 00:00 in Pacific time."""
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)

def _read_weather_history():
    try:
        with WEATHER_HISTORY_PATH.open("r", encoding="utf-8") as handle:
            saved = json.load(handle)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def _write_weather_history(data):
    temp_path = WEATHER_HISTORY_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    temp_path.replace(WEATHER_HISTORY_PATH)

def _merge_weekly_weather(old_data, new_towns, now):
    """Keep already-seen periods across this week and next week, including past hours.

    Fresh forecast values replace future/current periods. Old periods remain visible
    until the two-week display window rolls forward on Monday midnight.
    """
    week_start, week_end = _week_bounds(now)
    week_end += timedelta(days=7)
    previous = (old_data or {}).get("towns", {})
    merged = {}
    for town in WEATHER_TOWNS:
        by_time = {}
        for period in previous.get(town, []) if isinstance(previous, dict) else []:
            stamp = period.get("startTime")
            if not stamp:
                continue
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=PACIFIC)
                else:
                    dt = dt.astimezone(PACIFIC)
            except (ValueError, TypeError):
                continue
            if week_start <= dt < week_end:
                by_time[stamp] = period
        for period in new_towns.get(town, []):
            stamp = period.get("startTime")
            if not stamp:
                continue
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=PACIFIC)
                else:
                    dt = dt.astimezone(PACIFIC)
            except (ValueError, TypeError):
                continue
            if week_start <= dt < week_end:
                # Preserve the saved snapshot for periods already passed; refresh the rest.
                if dt < now and stamp in by_time:
                    continue
                by_time[stamp] = period
        merged[town] = sorted(by_time.values(), key=lambda x: x.get("startTime", ""))
    return merged

def get_weather(force=False):
    now = datetime.now(PACIFIC)
    with lock:
        if (not force and weather_cache["data"] and weather_cache["fetched"]
                and (now - weather_cache["fetched"]).total_seconds() < CACHE_SECONDS):
            return weather_cache["data"]
        saved = _read_weather_history()
        towns, errors = {}, {}
        for name, (lat, lon) in WEATHER_TOWNS.items():
            try:
                towns[name] = fetch_nws_hourly(lat, lon)
            except Exception as exc:
                errors[name] = str(exc)
                towns[name] = []
        merged_towns = _merge_weekly_weather(saved, towns, now)
        if not any(merged_towns.values()):
            raise RuntimeError("The National Weather Service did not return any hourly forecasts. Please try again shortly.")
        data = {"towns": merged_towns, "errors": errors, "updated": now.isoformat(),
                "source": "Open-Meteo hourly forecast (16-day model forecast)",
                "status": "partial" if errors else "live"}
        _write_weather_history(data)
        weather_cache.update(fetched=now, data=data)
        return data

def request_noaa(product, begin_date, end_date):
    params = {
        "begin_date": begin_date.strftime("%Y%m%d"),
        "end_date": end_date.strftime("%Y%m%d"),
        "station": STATION,
        "product": product,
        "datum": "MLLW",
        "time_zone": "lst_ldt",
        "units": "english",
        "format": "json",
    }
    if product == "predictions":
        params["interval"] = "6"
    response = requests.get(NOAA_API, params=params, timeout=25)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", str(payload["error"])))
    return payload

def normalize(rows):
    points = []
    for row in rows or []:
        try:
            # NOAA returns station-local date strings, e.g. "2026-10-21 08:24".
            stamp = row.get("t")
            value = float(row.get("v"))
            if stamp:
                points.append({"time": stamp, "height": value})
        except (TypeError, ValueError):
            continue
    return points

def get_tides(force=False):
    now = datetime.now(PACIFIC)
    with lock:
        if (not force and cache["data"] and cache["fetched"]
                and (now - cache["fetched"]).total_seconds() < CACHE_SECONDS):
            return cache["data"]

        # Show the current Monday-through-Sunday week in Humboldt Bay local time.
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=14)  # Fetch two weeks so the graph can advance its date window without running out of NOAA predictions.
        pred_payload = request_noaa("predictions", start, end)
        pred = normalize(pred_payload.get("predictions", []))
        if not pred:
            raise RuntimeError("NOAA did not return tide predictions for station 9418767.")

        # Actual measured water levels: request only through today's date because
        # observations cannot exist in the future. NOAA may return fewer readings
        # than predictions and can lag behind the current time.
        observed = []
        observation_error = None
        try:
            observed_payload = request_noaa("water_level", start, now)
            observed = normalize(observed_payload.get("data", []))
        except Exception as exc:
            # Preserve the real prediction line if the observation service is
            # temporarily unavailable; never substitute fabricated observations.
            observation_error = str(exc)

        data = {
            "predicted": pred,
            "observed": observed,
            "observation_error": observation_error,
            "source": "NOAA CO-OPS station 9418767 — North Spit, Humboldt Bay",
            "updated": now.isoformat(),
            "status": "live" if observation_error is None else "partial",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
        cache.update(fetched=now, data=data)
        return data

@app.route("/robots.txt")
def robots_txt():
    """Tell search engines which public areas may be crawled."""
    body = """User-agent: *
Disallow: /admin
Disallow: /api/
Disallow: /login/
Disallow: /logout
Disallow: /community-photo-file/
Disallow: /admin-private-photo/
Disallow: /fishing-story-image/
Disallow: /fishing-story-status/

Sitemap: https://fishhumboldt.com/sitemap.xml
"""
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """XML sitemap containing the public, indexable Fish Humboldt pages."""
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.sax.saxutils import escape

    base = "https://fishhumboldt.com"
    public_paths = [
        "/", "/tides", "/moon-cycle", "/species", "/rivers", "/bay",
        "/fishing-reports", "/weather", "/wind", "/best-days", "/community-photos",
        "/fishing-stories",
    ]
    urlset = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    today = datetime.now(PACIFIC).date().isoformat()
    for path in public_paths:
        item = SubElement(urlset, "url")
        SubElement(item, "loc").text = base + path
        SubElement(item, "lastmod").text = today
    xml = tostring(urlset, encoding="utf-8", xml_declaration=True)
    return app.response_class(xml, mimetype="application/xml")


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/tides")
def tides_page():
    return render_template("tides.html")

@app.route("/moon-cycle")
def moon_cycle_page():
    """Show two Monday-Sunday weeks of Sun/Moon data from SunriseSunset.io."""
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(tz)
    this_monday = now.date() - timedelta(days=now.weekday())
    range_end = this_monday + timedelta(days=13)
    params = {
        "lat": 40.8021,
        "lng": -124.1637,
        "date_start": this_monday.isoformat(),
        "date_end": range_end.isoformat(),
        "timezone": "America/Los_Angeles",
        "time_format": "12",
    }
    try:
        response = requests.get("https://api.sunrisesunset.io/json", params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "OK" or not isinstance(payload.get("results"), list):
            raise ValueError(payload.get("error") or "The astronomy service returned an unexpected response.")
        by_date = {row.get("date"): row for row in payload["results"] if row.get("date")}
        if len(by_date) < 14:
            raise ValueError("The astronomy service did not return all 14 requested dates.")

        weeks = []
        for week_index in range(2):
            week_start = this_monday + timedelta(days=week_index * 7)
            days = []
            for offset in range(7):
                day = week_start + timedelta(days=offset)
                row = by_date.get(day.isoformat(), {})
                sunrise_az = row.get("sunrise_azimuth")
                sunset_az = row.get("sunset_azimuth")
                days.append({
                    "date": day.strftime("%a, %b %d").replace(" 0", " "),
                    "iso": day.isoformat(),
                    "sunrise": _api_sun_event(row.get("sunrise"), sunrise_az),
                    "sunset": _api_sun_event(row.get("sunset"), sunset_az),
                    "moonrise": _api_moon_event(row.get("moonrise")),
                    "moonset": _api_moon_event(row.get("moonset")),
                    "phase": row.get("moon_phase") or "Unavailable",
                    "illumination": round(float(row.get("moon_illumination", 0) or 0), 1),
                })
            weeks.append({
                "start": week_start.strftime("%b %d").replace(" 0", " "),
                "end": (week_start + timedelta(days=6)).strftime("%b %d, %Y").replace(" 0", " "),
                "days": days,
            })
        generated_at = now.strftime("%a, %b %d, %Y at %I:%M %p %Z").replace(" 0", " ")
        return render_template("moon_cycle.html", error=None, weeks=weeks, generated_at=generated_at)
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        app.logger.warning("Moon Cycle API request failed: %s", exc)
        return render_template(
            "moon_cycle.html",
            error="We couldn't load astronomy data from SunriseSunset.io right now. Check your internet connection and refresh the page.",
            weeks=[], generated_at=None,
        ), 502


def _compass_direction(degrees):
    labels = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return labels[int((float(degrees) % 360 + 11.25) // 22.5) % 16]


def _api_sun_event(time_value, azimuth):
    if not time_value:
        return None
    event = {"time": str(time_value)}
    if azimuth is not None:
        degrees = round(float(azimuth), 1)
        event.update({"azimuth": degrees, "direction": _compass_direction(degrees), "degrees": degrees})
    return event


def _api_moon_event(time_value):
    # SunriseSunset.io does not provide moonrise/moonset azimuth bearings.
    return {"time": str(time_value)} if time_value else None


@app.route("/species")
def species_page():
    return render_template("species.html")


# Practical species profiles for the Humboldt County / Northern California region.
# Seasonal and harvest rules change; every profile links to the current CDFW regulations.
SPECIES_PROFILES = {
    "steelhead": {
        "name":"Steelhead", "scientific":"Oncorhynchus mykiss", "category":"River Species",
        "waters":"Eel River watershed, Mad River, Van Duzen River, Trinity River, and other suitable North Coast tributaries; access and presence vary by reach.",
        "method":"Drift-fish or swing presentations through current seams, tailouts, and deeper holding water. Cover water carefully and avoid disturbing spawning fish or redds.",
        "bait":"Where legal: roe or other permitted natural bait; artificial options include small spoons, spinners, beads, jigs, and flies. Some waters require artificial lures or barbless hooks.",
        "legal":"Not legal to retain from ocean waters. Freshwater fishing and retention depend on the exact river reach, run, hatchery/wild status, dates, gear rules, and any emergency closure. A steelhead report card may be required. Never assume a fish is keepable.",
        "timing":"North Coast winter runs are commonly encountered from late fall through spring, with timing varying by watershed and rainfall. The legal fishing window can be much shorter than the run itself.",
        "slug":"steelhead"},
    "chinook-salmon": {
        "name":"Chinook Salmon", "scientific":"Oncorhynchus tshawytscha", "category":"River and Ocean Species",
        "waters":"Eel, Mad, Van Duzen, Trinity and Klamath river systems, plus adjacent ocean waters when an ocean season is open.",
        "method":"In open river reaches, anglers commonly drift roe or fish plugs/spoons where allowed; in the ocean, trolling with suitable salmon gear is common. Match tackle to current and water clarity.",
        "bait":"Roe/roe clusters, plugs, spinners, spoons, and trolling lures can work where permitted. Follow hook, bait, barbless-hook, and gear restrictions for the exact reach.",
        "legal":"Retention is only allowed in specifically open waters and dates, and size, bag, gear, and report-card rules apply. Ocean salmon seasons are area-specific and can change in-season. Check CDFW before each trip.",
        "timing":"Runs often occur in late summer through fall, with timing differing among rivers and run types. Ocean seasons are set annually and may have short openings.",
        "slug":"chinook-salmon"},
    "coho-salmon": {
        "name":"Coho Salmon", "scientific":"Oncorhynchus kisutch", "category":"River Species",
        "waters":"Some North Coast coastal streams and suitable tributaries; local populations and access are limited and conservation-sensitive.",
        "method":"Do not target unless current rules explicitly allow fishing in that exact water. Where lawful, use light, controlled presentations and avoid disturbing spawning fish.",
        "bait":"Only methods explicitly permitted by the current reach-specific rules; roe, small spinners, spoons, and flies may be used in some open freshwater fisheries.",
        "legal":"Coho retention is prohibited in California ocean fisheries. Freshwater rules are highly location- and season-specific and may prohibit targeting or retention. Identify carefully and release immediately if required.",
        "timing":"Runs commonly enter coastal streams in fall and early winter, but the run does not mean the river is legally open.",
        "slug":"coho-salmon"},
    "pacific-lamprey": {
        "name":"Pacific Lamprey", "scientific":"Entosphenus tridentatus", "category":"River Species",
        "waters":"Eel, Mad, Trinity, Klamath and other connected North Coast river systems, including estuary-to-river migration corridors.",
        "method":"This is not a typical recreational angling target. Observe without handling and avoid disturbing migration or spawning habitat.",
        "bait":"No recreational bait or lure recommendation; it is not a standard sport-fishing species.",
        "legal":"Do not assume harvest is allowed. Check CDFW rules and any applicable tribal, watershed, or conservation protections before any take; the guide recommends observation only.",
        "timing":"Adult migrations vary by watershed and can occur in different seasons; ammocoetes live buried in stream sediment for years.",
        "slug":"pacific-lamprey"},
    "white-sturgeon": {
        "name":"White Sturgeon", "scientific":"Acipenser transmontanus", "category":"River Species",
        "waters":"White sturgeon are primarily associated with major estuaries and large river systems in California; do not assume they are present throughout every Humboldt County river reach.",
        "method":"Check current regulations before targeting. If a fishery is open, anglers generally use heavy bottom rigs in suitable deep channels and handle large fish carefully.",
        "bait":"Where expressly permitted, natural baits such as eel, shrimp, or fish baits are commonly used; confirm bait and gear rules first.",
        "legal":"Sturgeon regulations are unusually strict and can change. Confirm current CDFW rules for the specific water, including any moratorium, tag/report-card, size, and retention rules. If unsure, do not target or keep one.",
        "timing":"Availability depends on water temperature, flows, and migration. There is no single Humboldt-wide season that guarantees legal harvest.",
        "slug":"white-sturgeon"},
    "green-sturgeon": {
        "name":"Green Sturgeon", "scientific":"Acipenser medirostris", "category":"River and Bay Species",
        "waters":"Large coastal river systems and estuaries, including coastal migration corridors; occurrence varies by watershed and season.",
        "method":"Do not target. If accidentally caught, minimize handling and release promptly and carefully, following current agency guidance.",
        "bait":"No bait or lure recommendation because this species must not be targeted or retained.",
        "legal":"PROTECTED — take and possession are prohibited under California ocean sport-fishing regulations. Do not keep one. Check current freshwater rules and report accidental captures to the appropriate wildlife agency if advised.",
        "timing":"Migration into freshwater is seasonal and varies by population; seasonal presence does not make fishing for it legal.",
        "slug":"green-sturgeon"},
    "sacramento-pikeminnow": {
        "name":"Sacramento Pikeminnow", "scientific":"Ptychocheilus grandis", "category":"River Species",
        "waters":"Warm, slower pools and runs in suitable California river reaches, including parts of the Eel River watershed; local distribution varies.",
        "method":"Use light spinning tackle in pools, current seams, and deeper runs. Small lures or bait fished near the bottom can draw strikes.",
        "bait":"Small worms or other locally permitted bait; small spinners, spoons, and minnow-shaped lures are common options.",
        "legal":"Generally treated as a freshwater fish, but license requirements, special watershed rules, and any local restrictions still apply. Check current CDFW rules and identify it correctly.",
        "timing":"Often most active in warmer months and during low-to-moderate flows; access and water conditions matter more than a single run date.",
        "slug":"sacramento-pikeminnow"},
    "flounder": {
        "name":"Flounder", "scientific":"Several possible flatfish species", "category":"Bay Species",
        "waters":"Sandy or muddy bottoms in Humboldt Bay, channels, and nearby coastal waters where flatfish occur.",
        "method":"Fish close to the bottom with a light sinker and a slow retrieve or drift; work channel edges and sandy flats.",
        "bait":"Small pieces of shrimp, squid, or fish bait and small soft-plastic grubs can be effective where permitted.",
        "legal":"“Flounder” can refer to multiple species with different rules. Identify the exact fish and check size, bag, and method regulations before retaining it.",
        "timing":"Can be encountered year-round where present; tides, water temperature, and seasonal movement influence activity.",
        "slug":"flounder"},
    "rockfish": {
        "name":"Rockfish", "scientific":"Sebastes spp. (multiple species)", "category":"Bay Species",
        "waters":"Rocky structure and nearby coastal waters around jetties and reefs; species differ by depth and habitat.",
        "method":"Use a suitable bottom rig or small metal jig over rocky structure, keeping enough contact to find the bottom without snagging continuously. Use a descending device from a vessel when required.",
        "bait":"Squid strips, shrimp, fish strips, small shrimp flies, and metal jigs are common choices where legal.",
        "legal":"Rules vary by rockfish species, management area, depth, season, bag limit, and protected-species identification. Some species may not be retained. Check current CDFW ocean regulations and identify each fish before keeping it.",
        "timing":"Season and depth access vary annually; check the current North Coast groundfish season and closures before leaving shore.",
        "slug":"rockfish"},
    "lingcod": {
        "name":"Lingcod", "scientific":"Ophiodon elongatus", "category":"Bay and Ocean Species",
        "waters":"Rocky reefs, pinnacles, kelp-associated structure, and hard bottom along the Northern California coast; more likely in ocean habitat than the inner bay.",
        "method":"Fish jigs or swimbaits close to rocky structure and retrieve slowly with pauses. Use tackle strong enough to lift a fish away from snags.",
        "bait":"Large soft-plastic swimbaits, metal jigs, and permitted fish or squid baits.",
        "legal":"Retention is allowed only during open groundfish seasons and within current size and bag limits. Depth restrictions and area closures can apply; check the latest CDFW ocean regulations.",
        "timing":"North Coast boat-based groundfish seasons are often open in spring through year-end, but dates and depth access can change. Verify the current year's rules.",
        "slug":"lingcod"},
    "chinook-salmon-ocean": {
        "name":"Salmon (Chinook)", "scientific":"Oncorhynchus tshawytscha", "category":"Ocean Species",
        "waters":"Open coastal waters off the North Coast during an announced salmon season; nearshore presence varies with migration and ocean conditions.",
        "method":"When the ocean fishery is open, trolling at the right depth and speed is common. Follow the exact area, gear, and landing rules.",
        "bait":"Herring or other permitted trolling bait and salmon spoons/plugs; use only tackle and hook configurations allowed by current rules.",
        "legal":"Ocean retention is only permitted on announced open dates and within the applicable area, size, bag, and possession limits. In-season closures may occur. Check CDFW's current ocean salmon page before every trip.",
        "timing":"Ocean seasons are set by management area and year. Do not rely on a general spring/summer/fall estimate; check current openings.",
        "slug":"chinook-salmon"},
    "pacific-halibut": {
        "name":"Pacific Halibut", "scientific":"Hippoglossus stenolepis", "category":"Ocean Species",
        "waters":"Sandy and mixed bottoms offshore of the North Coast, usually in deeper water than California halibut.",
        "method":"Drift or anchor over suitable bottom and keep bait near the seabed; use enough weight to stay in contact with the bottom and account for current.",
        "bait":"Herring, squid, octopus, or other permitted fish baits; large jigs can also work.",
        "legal":"California's 2026 season is listed by CDFW as opening April 1; north of Point Arena it is scheduled through November 15, subject to in-season closure/quota updates. One-fish bag/possession limit and no minimum size are listed by CDFW. Verify current status before fishing.",
        "timing":"The 2026 recreational season opens April 1, but the fishery may close early if quota is reached. Check the CDFW Pacific halibut page immediately before a trip.",
        "slug":"pacific-halibut"},
    "california-halibut": {
        "name":"California Halibut", "scientific":"Paralichthys californicus", "category":"Bay and Ocean Species",
        "waters":"Sandy flats, channels, and nearshore coastal waters; more common in central/southern California bays, with local North Coast presence variable.",
        "method":"Drift or slow-troll just above sandy bottom, or cast and retrieve soft plastics across flats and channel edges.",
        "bait":"Live anchovy or other permitted baitfish, squid, and soft-plastic swimbaits or jerkbaits.",
        "legal":"Check current California size, bag, and method rules and any local closures. Do not confuse this species with Pacific halibut, which has a separate season and rules.",
        "timing":"Can be caught in suitable habitat across much of the year; warmer periods and baitfish presence can improve opportunities. This is not a guarantee of local availability.",
        "slug":"california-halibut"},
    "black-rockfish": {
        "name":"Black Rockfish", "scientific":"Sebastes melanops", "category":"Ocean Species",
        "waters":"Kelp edges, rocky reefs, and nearshore to offshore structure along the North Coast.",
        "method":"Fish a light bottom rig, shrimp fly, or metal jig over reefs and kelp edges; keep gear close to structure but avoid repeated snags.",
        "bait":"Squid strips, shrimp, small fish strips, shrimp flies, and metal jigs where permitted.",
        "legal":"May be retained only within current groundfish season, area/depth rules, and bag limits. Identify rockfish accurately because some species are prohibited and sub-limits can apply.",
        "timing":"Seasonal boat and depth rules vary by year; check CDFW's current ocean regulations for the North Coast before departing.",
        "slug":"black-rockfish"},
    "copper-rockfish": {
        "name":"Copper Rockfish", "scientific":"Sebastes caurinus", "category":"Ocean Species",
        "waters":"Rocky reefs, kelp-associated structure, and hard bottom, often in relatively shallow to moderate depths.",
        "method":"Use a small bottom rig or jig around reef edges; lower carefully and retrieve steadily to reduce snags.",
        "bait":"Squid, shrimp, fish strips, shrimp flies, and small metal jigs where permitted.",
        "legal":"Subject to current groundfish season, depth/area closures, and rockfish bag or sub-bag limits. Confirm the current CDFW rules and use careful species identification.",
        "timing":"Availability follows habitat and ocean conditions; legal access depends on current annual groundfish rules.",
        "slug":"copper-rockfish"},
    "vermilion-rockfish": {
        "name":"Vermilion Rockfish", "scientific":"Sebastes miniatus", "category":"Ocean Species",
        "waters":"Rocky reefs and hard bottom, often deeper than many common nearshore species.",
        "method":"Fish weighted bait rigs or jigs over hard bottom and use controlled retrieves. From a vessel, follow rockfish barotrauma requirements and carry a descending device when required.",
        "bait":"Squid, fish strips, shrimp flies, and metal jigs where permitted.",
        "legal":"Groundfish season, depth restrictions, bag limits, and species sub-limits apply; some similar-looking rockfish are prohibited. Check current CDFW rules and identify the fish carefully.",
        "timing":"Season and depth access vary by year and management area. Verify current regulations before fishing.",
        "slug":"vermilion-rockfish"},
    "albacore-tuna": {
        "name":"Albacore Tuna", "scientific":"Thunnus alalunga", "category":"Ocean Species",
        "waters":"Open ocean well offshore of the North Coast; location shifts with warm water, bait, and ocean conditions.",
        "method":"Commonly targeted by trolling feathers/plugs or locating schools and using casting gear. Offshore trips require appropriate vessel safety, weather planning, and experienced crews.",
        "bait":"Trolling feathers, tuna plugs, cedar plugs, and small baitfish imitations; live bait where available and lawful.",
        "legal":"Recreational retention is generally regulated by ocean sport-fishing rules; confirm current bag limits, licensing, and any emergency notices before departure.",
        "timing":"Often a late-summer to fall North Coast opportunity, but it depends strongly on offshore water temperatures and albacore migration.",
        "slug":"albacore-tuna"},
    "redtail-surfperch": {
        "name":"Redtail Surfperch", "scientific":"Amphistichus rhodoterus", "category":"Ocean Species",
        "waters":"Sandy open-coast beaches and surf zones along Northern California.",
        "method":"Cast beyond breaking waves and fish the troughs and holes, keeping the bait moving slowly with the surf. Watch waves and never turn your back on the ocean.",
        "bait":"Sand crabs, small pieces of shrimp or worms, and small surfperch rigs where permitted; small grubs can also work.",
        "legal":"Check current surfperch rules for species identification, bag limits, and seasonal restrictions. The annual San Francisco Bay surfperch closure does not necessarily describe open-coast beach rules; verify the exact location.",
        "timing":"Can be caught throughout much of the year; surf, sand crab availability, and beach conditions affect success.",
        "slug":"redtail-surfperch"},
    "kelp-greenling": {
        "name":"Kelp Greenling", "scientific":"Hexagrammos decagrammus", "category":"Ocean Species",
        "waters":"Kelp forests, rocky reefs, and nearshore hard bottom along the North Coast.",
        "method":"Fish small bait rigs or jigs close to rocks and kelp edges; use a controlled retrieve to avoid snags.",
        "bait":"Squid strips, shrimp, small fish pieces, and small jigs where permitted.",
        "legal":"Part of California's groundfish/rockfish, cabezon, and greenling management rules. Open season, bag/sub-bag limits, and depth rules may apply; verify current CDFW regulations.",
        "timing":"Can be encountered in suitable habitat year-round, but legal boat access and depth rules are seasonal and area-specific.",
        "slug":"kelp-greenling"},
    "leopard-shark": {
        "name":"Leopard Shark", "scientific":"Triakis semifasciata", "category":"Bay and Ocean Species",
        "waters":"Shallow coastal bays, sandy flats, estuaries, and nearshore waters; local abundance varies.",
        "method":"Fish a simple bottom rig in sandy channels and flats, handle the animal with care, and release promptly if not keeping it or if rules require release.",
        "bait":"Squid, anchovy, sardine, or other permitted fish baits on a sturdy bottom rig.",
        "legal":"Confirm current shark-specific size/bag rules and local closures before retention. Correct identification matters because shark species have different protections; follow handling guidance and health advisories.",
        "timing":"Often encountered in warmer months in shallow protected waters, though seasonal presence varies by bay and year.",
        "slug":"leopard-shark"},
    "bat-ray": {
        "name":"Bat Ray", "scientific":"Myliobatis californica", "category":"Bay and Ocean Species",
        "waters":"Sandy and muddy bottoms in Humboldt Bay and other protected coastal bays and nearshore waters.",
        "method":"Fish near the bottom with sturdy tackle and a smooth drag. Use caution around the tail spine; do not lift a large ray by its tail.",
        "bait":"Squid, shrimp, clams, or fish pieces on a bottom rig where permitted.",
        "legal":"Check current rules and advisories for the exact location and method. If releasing, avoid the tail spine, minimize handling, and use appropriate tools.",
        "timing":"May be encountered year-round in suitable habitat; tides and water temperature can influence activity.",
        "slug":"bat-ray"},
    "surfperch": {
        "name":"Surfperch", "scientific":"Multiple surfperch species", "category":"Bay Species",
        "waters":"Bay shorelines, piers, and nearby surf zones; the exact species varies by habitat.",
        "method":"Use a light surf rig and fish the bottom near pilings, sandy troughs, and current seams.",
        "bait":"Sand crabs, shrimp, worms, and small grubs where permitted.",
        "legal":"Different surfperch species and locations can have different rules. San Francisco Bay has a seasonal surfperch closure; confirm whether it applies to the exact water and species before keeping fish.",
        "timing":"Often available year-round, with species and feeding activity changing with season and conditions.",
        "slug":"surfperch"},
    "sardines": {
        "name":"Sardines", "scientific":"Pacific sardine (Sardinops sagax)", "category":"Bay Species",
        "waters":"Coastal waters and sometimes estuaries when schools move inshore.",
        "method":"Locate schools visually or with sonar; use small hooks or a sabiki-style rig where legal and appropriate.",
        "bait":"Small shiny hooks or tiny bait imitations; regulations may govern gear and take.",
        "legal":"Check current baitfish/small pelagic regulations, gear rules, and local restrictions. Do not assume all schooling fish in a mixed catch are the same species.",
        "timing":"Schooling and availability can change quickly through the year with ocean conditions and prey movements.",
        "slug":"sardines"},
    "anchovies": {
        "name":"Anchovies", "scientific":"Northern anchovy (Engraulis mordax)", "category":"Bay Species",
        "waters":"Coastal waters, bays, and estuaries where schools are present.",
        "method":"When recreational take is allowed, use small hooks or a bait-catching rig around visible schools.",
        "bait":"Tiny baitfish imitations or small hooks appropriate to the target fish and current gear rules.",
        "legal":"Check current regulations for baitfish take, gear, and local restrictions. Confirm identification before retaining a catch.",
        "timing":"School presence is variable and depends on plankton, water temperature, and seasonal ocean conditions.",
        "slug":"anchovies"},
    "red-rock-crab": {
        "name":"Red Rock Crab", "scientific":"Cancer productus", "category":"Bay Species",
        "waters":"Rocky shorelines, pilings, jetties, and estuary structure.",
        "method":"Use legal crab traps or hoop nets from permitted locations; mark gear correctly and check it regularly.",
        "bait":"Fish carcasses or other permitted crab-trap bait.",
        "legal":"Crab seasons, sex/size limits, gear rules, trap markings, and marine closures apply. Check current CDFW crab regulations and any health advisories before consuming crab.",
        "timing":"Availability varies with season and location; check current crab-season notices and local advisories.",
        "slug":"red-rock-crab"},
    "dungeness-crab": {
        "name":"Dungeness Crab", "scientific":"Metacarcinus magister", "category":"Bay Species",
        "waters":"Sandy and muddy bottoms in bays and nearshore coastal waters.",
        "method":"Use legal crab traps or hoop nets where allowed; follow trap marking, escape-port, and gear rules.",
        "bait":"Fish carcasses or other permitted bait secured inside a legal trap.",
        "legal":"Season opening dates, minimum size, sex restrictions, gear rules, and health advisories can vary. Check current CDFW recreational crab regulations and local closures before setting gear or eating crab.",
        "timing":"Often a cool-season fishery, but exact opening dates and closures are set by current rules and public-health advisories.",
        "slug":"dungeness-crab"},
    "rainbow-trout": {
        "name":"Rainbow Trout", "scientific":"Oncorhynchus mykiss", "category":"Lake and Lagoon Species",
        "waters":"Reported at Freshwater Lagoon and Ruth Lake. Rainbow trout and steelhead are the same species in different life-history forms; trout reports at coastal lagoons may instead refer to steelhead or coastal cutthroat trout.",
        "method":"In waters where trout fishing is open, try small spinners, spoons, flies, or bait permitted by the current rules. Check access and local regulations before fishing any lagoon or reservoir.",
        "bait":"Small artificial lures, flies, or locally permitted natural baits; use only methods allowed for the specific water.",
        "legal":"Confirm current CDFW regulations, stocking notices, access restrictions, and any seasonal closures for the exact waterbody. Presence does not mean fishing or retention is currently allowed.",
        "timing":"Catch patterns vary with stocking, water temperature, water levels, and season.", "slug":"rainbow-trout"},
    "brown-trout": {
        "name":"Brown Trout", "scientific":"Salmo trutta", "category":"Lake and Lagoon Species",
        "waters":"German brown trout are reported at Freshwater Lagoon. A dependable species list could not be confirmed for Sweasey Lake or Mosquito Lake, so this guide does not claim brown trout are present there.",
        "method":"Where trout fishing is open, use light spinning tackle or fly gear and fish suitable deeper edges and structure. Confirm access and the current regulations for the specific waterbody.",
        "bait":"Small spinners, spoons, flies, or permitted natural bait depending on local rules.",
        "legal":"Check current California regulations and local access rules before fishing or retaining trout. Do not infer presence at unconfirmed lakes.",
        "timing":"Activity varies with water temperature, depth, and seasonal conditions.", "slug":"brown-trout"},
    "coastal-cutthroat-trout": {
        "name":"Coastal Cutthroat Trout", "scientific":"Oncorhynchus clarkii clarkii", "category":"Lake and Lagoon Species",
        "waters":"Associated with Big Lagoon and Stone Lagoon, though occurrence and access can change with lagoon conditions and connection to coastal streams. This is not confirmation of presence in every lagoon.",
        "method":"Treat as a conservation-sensitive native trout. Observe and identify carefully; only fish where current regulations explicitly permit it, and avoid disturbing spawning habitat.",
        "bait":"No bait recommendation should override reach-specific gear restrictions; use only legal methods where fishing is open.",
        "legal":"Check current CDFW regulations, lagoon closures, and handling guidance before targeting or retaining trout. Release fish when required or when identification is uncertain.",
        "timing":"Occurrence depends on lagoon openings, stream connectivity, flows, and seasonal movement.", "slug":"coastal-cutthroat-trout"},
    "largemouth-bass": {
        "name":"Largemouth Bass", "scientific":"Micropterus salmoides", "category":"Lake and Lagoon Species",
        "waters":"Reported at Freshwater Lagoon and Ruth Lake. Do not assume the species occurs in Big Lagoon, Stone Lagoon, Dry Lagoon, Sweasey Lake, or Mosquito Lake without a reliable waterbody-specific record.",
        "method":"Where present and fishing is open, work weed edges, submerged structure, and drop-offs with steady, controlled retrieves.",
        "bait":"Soft plastics, small jigs, crankbaits, spinnerbaits, or other legal bass lures.",
        "legal":"Check current freshwater regulations and access restrictions for the exact lake or lagoon.",
        "timing":"Warm periods often increase activity, but water conditions and season affect results.", "slug":"largemouth-bass"},
    "smallmouth-bass": {
        "name":"Smallmouth Bass", "scientific":"Micropterus dolomieu", "category":"Lake and Lagoon Species",
        "waters":"Reported at Ruth Lake. A reliable source confirming smallmouth bass in the named coastal lagoons was not found.",
        "method":"Where present and legal to fish, try rocky edges, points, and drop-offs with small jigs or finesse presentations.",
        "bait":"Small jigs, soft plastics, crankbaits, and other legal bass lures.",
        "legal":"Verify current rules and local access before fishing; this profile does not assert presence in unconfirmed waters.",
        "timing":"Activity varies with water temperature, depth, and seasonal conditions.", "slug":"smallmouth-bass"},
    "bluegill": {
        "name":"Bluegill", "scientific":"Lepomis macrochirus", "category":"Lake and Lagoon Species",
        "waters":"Reported at Ruth Lake and Freshwater Lagoon. No confirmed listing is made for the other named lakes and lagoons.",
        "method":"Where present, use light tackle near sheltered edges and vegetation, while following local access and gear rules.",
        "bait":"Small worms or other permitted bait, tiny jigs, and small artificial presentations.",
        "legal":"Check current freshwater regulations and waterbody-specific restrictions before fishing or keeping fish.",
        "timing":"Often most active in warmer water; conditions vary by season.", "slug":"bluegill"},
    "crappie": {
        "name":"Crappie", "scientific":"Pomoxis spp.", "category":"Lake and Lagoon Species",
        "waters":"Reported at Ruth Lake. A dependable public record for the named coastal lagoons could not be confirmed.",
        "method":"Where present, fish light jigs around submerged structure and deeper edges, subject to local rules.",
        "bait":"Small jigs, tiny spinners, or permitted natural bait.",
        "legal":"Confirm current regulations and exact-waterbody species records before planning a trip.",
        "timing":"Activity and location shift with water temperature and seasonal conditions.", "slug":"crappie"},
    "catfish": {
        "name":"Catfish", "scientific":"Catfish species not confirmed", "category":"Lake and Lagoon Species",
        "waters":"Catfish are listed in local fishing references for Ruth Lake and are also reported for Freshwater Lagoon. The exact species may differ by source; this page does not assume a particular catfish species.",
        "method":"Where catfish fishing is open, anglers commonly fish near the bottom in suitable habitat. Confirm access and local rules first.",
        "bait":"Use only legal bait for the waterbody; common catfish baits include worms or prepared bait where permitted.",
        "legal":"Verify current regulations, bait restrictions, and local access requirements. Confirm the fish's identification before keeping it.",
        "timing":"Catch patterns vary with water temperature, water level, and season.", "slug":"catfish"},
}

SPECIES_IMAGES = {
    "rainbow-trout": "https://commons.wikimedia.org/wiki/Special:FilePath/Oncorhynchus_mykiss.jpg",
    "brown-trout": "/static/brown-trout-user.png",
    "coastal-cutthroat-trout": "/static/coastal-cutthroat-trout-user.png",
    "largemouth-bass": "https://commons.wikimedia.org/wiki/Special:FilePath/Micropterus_salmoides.jpg",
    "smallmouth-bass": "https://commons.wikimedia.org/wiki/Special:FilePath/Micropterus_dolomieu.jpg",
    "bluegill": "https://commons.wikimedia.org/wiki/Special:FilePath/Lepomis_macrochirus.jpg",
    "crappie": "https://commons.wikimedia.org/wiki/Special:FilePath/Black_crappie.jpg",
    "catfish": "https://commons.wikimedia.org/wiki/Special:FilePath/Ictalurus_punctatus.jpg",
    "steelhead": "https://commons.wikimedia.org/wiki/Special:FilePath/Oncorhynchus_mykiss.jpg",
    "chinook-salmon": "/static/chinook-salmon.png", "coho-salmon": "/static/coho-salmon.png",
    "pacific-lamprey": "/static/pacific-lamprey.png", "white-sturgeon": "/static/white-sturgeon.png",
    "green-sturgeon": "/static/green-sturgeon.png", "sacramento-pikeminnow": "/static/sacramento-pikeminnow.png",
    "flounder": "/static/flounder.png", "rockfish": "/static/rockfish.png",
    "lingcod": "https://commons.wikimedia.org/wiki/Special:FilePath/Lingcod.jpg",
    "bat-ray": "/static/bat-ray.png", "leopard-shark": "https://commons.wikimedia.org/wiki/Special:FilePath/Leopard_shark.jpg",
    "surfperch": "/static/surfperch.png", "california-halibut": "/static/california-halibut.png",
    "sardines": "/static/sardines.png", "anchovies": "/static/anchovies.png",
    "red-rock-crab": "/static/red-rock-crab.png", "dungeness-crab": "/static/dungeness-crab.png",
    "pacific-halibut": "/static/ocean-pacific-halibut.png", "black-rockfish": "/static/rockfish.png",
    "copper-rockfish": "https://commons.wikimedia.org/wiki/Special:FilePath/Copper_Rockfish.jpg",
    "vermilion-rockfish": "/static/ocean-vermilion-rockfish.png",
    "albacore-tuna": "https://commons.wikimedia.org/wiki/Special:FilePath/Thunnus_alalunga.jpg",
    "redtail-surfperch": "/static/surfperch.png", "kelp-greenling": "/static/ocean-kelp-greenling.png",
}
for _slug, _profile in SPECIES_PROFILES.items():
    _profile["image"] = SPECIES_IMAGES.get(_slug, "/static/rockfish.png")

# Some cards use alternate labels for the same species profile.
SPECIES_ALIASES = {
    "stealhead": "steelhead",  # Friendly redirect for the common misspelling in the requested URL.
    "salmon-chinook": "chinook-salmon",
    "rockfish": "rockfish",
}

@app.route("/species/<slug>")
def species_detail(slug):
    slug = SPECIES_ALIASES.get(slug, slug)
    profile = SPECIES_PROFILES.get(slug)
    if profile is None:
        abort(404)
    if request.view_args and request.view_args.get("slug") == "salmon-chinook":
        profile = dict(profile, category="Ocean Species")
    return render_template("species_detail.html", fish=profile)

@app.route("/rivers")
def rivers_page():
    return render_template("rivers.html")

@app.route("/api/rivers")
def rivers_api():
    """Read the latest dated status row from CDFW's North Coast Low-Flow table."""
    source_url = "https://wildlife.ca.gov/Fishing/Inland/Low-Flow/North-Coast"
    now = datetime.now(PACIFIC).isoformat()
    # These are the actual columns in the CDFW table (not guessed nearby rivers).
    river_columns = ["Eel River", "South Fork Eel River", "Middle Fork Eel River",
                     "Van Duzen River", "Mad River", "Mattole River", "Redwood Creek",
                     "Smith River", "Mendocino Coast Streams"]
    try:
        from bs4 import BeautifulSoup
        response = requests.get(source_url, timeout=25, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HumboldtFishers/1.0; river-status-check)"
        })
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        chosen = None
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [" ".join(c.stripped_strings).strip() for c in rows[0].find_all(["th", "td"])]
            norm = [" ".join(h.lower().split()) for h in headers]
            if any(h == "date" for h in norm) and any("date and time of update" in h for h in norm):
                chosen = (headers, rows)
                break
        if not chosen:
            raise RuntimeError("CDFW page loaded, but its dated open/closed status table was not found. No status was guessed.")
        headers, rows = chosen
        indexes = {" ".join(h.lower().split()): i for i, h in enumerate(headers)}
        # Pick the newest row that contains a date and at least one explicit status.
        data_rows = []
        for row in rows[1:]:
            cells = [" ".join(c.stripped_strings).strip() for c in row.find_all(["th", "td"])]
            if len(cells) < len(headers):
                continue
            date_text = cells[indexes.get("date", 0)]
            if not date_text:
                continue
            data_rows.append((date_text, cells))
        if not data_rows:
            raise RuntimeError("CDFW status table was found, but no dated status rows were available.")
        # Use the row for today's Pacific date, not a future-effective row
        # that may already appear at the top of CDFW's table.
        today = datetime.now(PACIFIC).date()
        dated_rows = []
        for date_text, cells in data_rows:
            try:
                row_date = datetime.strptime(date_text, "%m/%d/%Y").date()
            except ValueError:
                continue
            dated_rows.append((row_date, date_text, cells))
        if not dated_rows:
            raise RuntimeError("CDFW status table dates could not be interpreted.")
        eligible_rows = [item for item in dated_rows if item[0] <= today]
        if eligible_rows:
            _, latest_date, latest = max(eligible_rows, key=lambda item: item[0])
        else:
            _, latest_date, latest = min(dated_rows, key=lambda item: item[0])
        results = []
        for river in river_columns:
            idx = next((i for i,h in enumerate(headers) if " ".join(h.lower().split()) == river.lower()), None)
            value = latest[idx].strip() if idx is not None and idx < len(latest) else ""
            normalized = value.casefold()
            if normalized == "open":
                status, label = "open", "Open"
                detail = "CDFW's latest low-flow table marks this water OPEN. Check the exact reach and all other fishing regulations before fishing."
            elif normalized == "closed":
                status, label = "closed", "Closed"
                detail = "CDFW's latest low-flow table marks this water CLOSED. Check the exact reach and update date before fishing."
            else:
                status, label = "unconfirmed", "Status unavailable"
                detail = "No explicit Open/Closed value was read for this column. Verify the official table directly."
            results.append({"name":river, "status":status, "label":label, "detail":detail,
                            "source":source_url, "source_value":value or "Not listed"})
        update_idx = next((i for i,h in enumerate(headers) if "date and time of update" in " ".join(h.lower().split())), None)
        update_time = latest[update_idx] if update_idx is not None and update_idx < len(latest) else "Not shown"
        return jsonify({"status":"live", "updated":now, "source":source_url,
                        "source_date":latest_date, "source_update":update_time,
                        "note":f"Read from CDFW's newest table row dated {latest_date}. Status is low-flow information only; other rules and specific reaches may differ.",
                        "rivers":results})
    except Exception as exc:
        return jsonify({"status":"unavailable", "updated":now, "error":str(exc),
                        "source":source_url, "note":"Could not reliably read the current official status table. No Open/Closed statuses are guessed.",
                        "rivers":[{"name":r, "status":"unconfirmed", "label":"Unable to verify",
                        "detail":"The latest official status could not be read automatically. Please check CDFW directly.", "source":source_url}
                        for r in river_columns]}), 502

@app.route("/api/river-flow")
def river_flow_api():
    """Fetch NOAA/NWS observed and forecast stage/flow for mapped Humboldt-area gauges."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    gauges = [
        {"name": "Eel River", "station": "SCOC1", "location": "Scotia", "url": "https://water.noaa.gov/gauges/scoc1"},
        {"name": "South Fork Eel River", "station": "MRNC1", "location": "Miranda", "url": "https://water.noaa.gov/gauges/mrnc1"},
        {"name": "Middle Fork Eel River", "station": "USGS-11473900", "location": "Middle Fork Eel near Dos Rios", "url": "https://waterdata.usgs.gov/monitoring-location/USGS-11473900/"},
        {"name": "Van Duzen River", "station": "11478500", "location": "near Bridgeville", "url": "https://water.noaa.gov/gauges/11478500"},
        {"name": "Mad River", "station": "ARCC1", "location": "Arcata", "url": "https://water.noaa.gov/gauges/arcc1"},
        {"name": "Mattole River", "station": "ETTC1", "location": "near Ettersburg", "url": "https://water.noaa.gov/gauges/ettc1"},
        {"name": "Redwood Creek", "station": "ORIC1", "location": "Orick", "url": "https://water.noaa.gov/gauges/oric1"},
        {"name": "Smith River", "station": "FTDC1", "location": "Fort Dick", "url": "https://water.noaa.gov/gauges/ftdc1"},
    ]
    base = "https://api.water.noaa.gov/nwps/v1/gauges/"
    headers = {"User-Agent": "HumboldtFishers/1.0 (river chart; NOAA NWPS API)"}
    def fetch(g):
        try:
            r = requests.get(base + g["station"] + "/stageflow", timeout=12, headers=headers)
            if r.status_code == 404 and g["station"].startswith("USGS-"):
                r = requests.get(base + g["station"].replace("USGS-", "") + "/stageflow", timeout=12, headers=headers)
            r.raise_for_status()
            return {**g, "ok": True, "data": r.json()}
        except Exception as exc:
            return {**g, "ok": False, "error": "NOAA data is temporarily unavailable for this gauge."}
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, g) for g in gauges]
        for future in as_completed(futures):
            results.append(future.result())
    by_name = {x["name"]: x for x in results}
    ordered = [by_name[g["name"]] for g in gauges]
    ordered.append({"name": "Mendocino Coast Streams", "station": "11468500", "location": "Noyo River near Fort Bragg (representative gauge only)", "url": "https://water.noaa.gov/gauges/11468500", "representative": True})
    return jsonify({"updated": datetime.now(PACIFIC).isoformat(), "source": "NOAA National Water Prediction Service", "gauges": ordered})

@app.route("/api/rivers/refresh")
def rivers_refresh_api():
    return rivers_api()

_BAY_REG_CHECK = {"checked_at": None, "status": "unknown", "species": {}}
_BAY_REG_CHECKED_TS = 0.0
_BAY_REG_CHECK_TTL_SECONDS = 24 * 60 * 60
_BAY_REG_LOCK = Lock()
_BAY_CDFW_URL = "https://wildlife.ca.gov/Fishing/Ocean/Regulations/Fishing-Map/SF-Bay"

@app.route("/api/bay-regulations-status")
def bay_regulations_status():
    """Read the official CDFW San Francisco Bay District regulations page and cache results for 24 hours."""
    global _BAY_REG_CHECKED_TS
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_ts = time.time()
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    if not _BAY_REG_CHECKED_TS or now_ts - _BAY_REG_CHECKED_TS >= _BAY_REG_CHECK_TTL_SECONDS:
        with _BAY_REG_LOCK:
            # Another request may have refreshed while this request waited for the lock.
            now_ts = time.time()
            if not _BAY_REG_CHECKED_TS or now_ts - _BAY_REG_CHECKED_TS >= _BAY_REG_CHECK_TTL_SECONDS:
                page = ""
                error = None
                try:
                    req = urllib.request.Request(
                        _BAY_CDFW_URL,
                        headers={"User-Agent": "HumboldtFishers/1.0 (CDFW regulation status checker; refresh every 24 hours)"},
                    )
                    with urllib.request.urlopen(req, timeout=20) as response:
                        if response.status != 200:
                            raise RuntimeError("CDFW returned HTTP " + str(response.status))
                        raw = response.read().decode("utf-8", "ignore")
                    raw = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
                    raw = re.sub(r"<[^>]+>", " ", raw)
                    raw = raw.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
                    page = re.sub(r"\s+", " ", raw).strip().lower()
                except Exception as exc:
                    error = str(exc)

                # States: open = green (source explicitly lists an open ocean season),
                # closed = red (source explicitly says closed / prohibited), check = yellow.
                # This page covers the San Francisco Bay District summary; exact water, MPA, gear, and species rules still apply.
                species = {}
                def add(slug, state, label, detail):
                    species[slug] = {"state": state, "label": label, "detail": detail, "source": _BAY_CDFW_URL}

                if page:
                    if "ocean salmon" in page and "closed in the klamath management zone for the remainder of 2026" in page:
                        add("chinook-salmon", "closed", "RED — Ocean salmon season closed in the Klamath Management Zone", "CDFW says the ocean salmon fishery is closed in the Klamath Management Zone for the remainder of 2026. This is an ocean rule; it does not decide separate Humboldt Bay or river fisheries. Do not target or retain ocean salmon in this closed zone.")
                    else:
                        add("chinook-salmon", "check", "YELLOW — Check the exact salmon fishery", "The current page did not clearly confirm an open season for this exact salmon fishery and location. Check the CDFW salmon season page and local river/bay rules before fishing or keeping a fish.")

                    if "the recreational fishery for california halibut" in page and "remains open year-round" in page:
                        add("california-halibut", "open", "GREEN — Ocean season open; size and bag limits apply", "CDFW says California halibut remains open year-round. North of Point Sur the daily bag and possession limit is 2 fish and the minimum size is 22 inches total length. Location, gear, and MPA restrictions still apply.")
                    if "pacific sanddab and other flatfish" in page and "open year-round" in page:
                        add("flounder", "check", "YELLOW — Identify the exact flatfish before keeping", "CDFW lists Pacific sanddab and specified flatfish (including butter sole, curlfin sole, flathead sole, rex sole, rock sole, and sand sole) as open year-round at all depths. Petrale sole and starry flounder are also listed open year-round with no depth restrictions or bag limits. This card groups several flatfish together, so identify the exact species and check its complete size, bag, and other rules before keeping it.")
                    if "open fishing seasons" in page and "rockfish" in page and "closed fishing seasons" in page:
                        add("rockfish", "open", "GREEN — Ocean rockfish season category open; species rules apply", "Rockfish is listed under CDFW's open North Coast ocean season categories. Exact species, depth, location, boat-versus-shore rules, bag limits, and marine protected areas can still restrict fishing or retention.")
                    if "the recreational fishery for surfperch" in page and "open year-round" in page:
                        add("surfperch", "open", "GREEN — Ocean season open; combined bag limit applies", "CDFW says surfperch is open year-round. The daily bag and possession limit is 20 fish combined across species (except shiner perch), with no more than 10 of any one species; redtail surfperch have a 10½-inch minimum. Confirm exact identification and local restrictions.")
                    if "the recreational fishery for all rock crab species" in page and "open year-round, statewide" in page:
                        add("red-rock-crab", "open", "GREEN — Rock crab season open; size and bag limits apply", "CDFW says rock crab, including red rock crab, is open year-round statewide with a 35-crab daily bag limit and 4-inch minimum size. Check current health advisories, legal gear, and any local restrictions before keeping crab.")
                    if "the season for dungeness crab" in page and "closed as of july 31, 2026" in page:
                        add("dungeness-crab", "closed", "RED — Dungeness crab season closed", "CDFW says the North Coast ocean Dungeness crab season closed July 31, 2026 and is scheduled to reopen November 7, 2026. Do not assume the scheduled date guarantees an opening: check the official page for any new delay or gear restriction on the day you go.")
                    elif "dungeness crab" in page and "closed" in page:
                        add("dungeness-crab", "closed", "RED — Dungeness crab closure shown by CDFW", "CDFW's current North Coast page shows Dungeness crab as closed. Check the official page for the current opening date and any in-season gear restrictions.")
                    if "green sturgeon" in page and "may not be removed from the water, taken, or possessed at any time" in page:
                        add("green-sturgeon", "closed", "RED — Protected; do not take or keep", "CDFW says green sturgeon may not be taken or possessed at any time and must be released immediately without removing it from the water.")
                    if "catch-and-release fishing only for white sturgeon" in page and "daily bag and possession limit for white sturgeon is zero (0) fish" in page:
                        add("white-sturgeon", "check", "YELLOW — Ocean catch-and-release only; zero retention", "CDFW states that white sturgeon fishing in ocean waters is catch-and-release only from October 1, 2026 through June 30, 2027, subject to the January 1–March 15 area closure. The daily and annual retention limits are zero. A Sturgeon Fishing Report Card and specific single-barbless-hook and handling rules apply. This ocean rule must not be treated as the rule for every San Francisco Bay, tidal river, or freshwater reach; check the exact waterbody and current sturgeon regulations before fishing.")
                    if "pacific herring" in page and "open year-round" in page:
                        add("anchovies", "check", "YELLOW — Anchovies not specifically verified", "CDFW's page specifically lists Pacific herring as open year-round, not anchovies. This check cannot confirm anchovy rules from that entry; verify the exact species and fishing method before keeping them.")
                    add("sardines", "check", "YELLOW — Exact sardine rule not confirmed by this summary", "The San Francisco Bay District summary does not provide a specific sardine rule in the text checked. Follow the linked full ocean sport fishing regulations before targeting or retaining sardines.")
                    add("bat-ray", "check", "YELLOW — Bat ray rule not specified on this summary", "The CDFW San Francisco Bay District summary does not give a specific recreational take, size, or bag rule for bat rays in the entries checked. This is not confirmation that a bat ray may be kept. Check the complete current California ocean sport fishing regulations and any local or marine protected area restrictions before targeting or retaining one.")
                    if "ocean salmon" in page and "closed in the klamath management zone for the remainder of 2026" in page:
                        add("coho-salmon", "closed", "RED — Ocean salmon season closed in the Klamath Management Zone", "CDFW lists the ocean salmon fishery as closed in the Klamath Management Zone for the remainder of 2026. Do not assume this answers separate bay or river rules; verify the exact fishery.")
                    else:
                        add("coho-salmon", "check", "YELLOW — Check exact salmon fishery", "This summary did not confirm a current open season for this exact salmon fishery and location. Check CDFW's salmon season rules before targeting or retaining coho.")

                # Fetch the North Coast page separately for Humboldt-area species.
                north_page = ""
                north_url = "https://wildlife.ca.gov/Fishing/Ocean/Regulations/Fishing-Map/Northern"
                try:
                    req = urllib.request.Request(
                        north_url,
                        headers={"User-Agent": "FishHumboldt/1.0 (CDFW regulation status checker; refresh every 24 hours)"},
                    )
                    with urllib.request.urlopen(req, timeout=20) as response:
                        if response.status != 200:
                            raise RuntimeError("CDFW Northern Region returned HTTP " + str(response.status))
                        north_raw = response.read().decode("utf-8", "ignore")
                    north_raw = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", north_raw, flags=re.I | re.S)
                    north_raw = re.sub(r"<[^>]+>", " ", north_raw)
                    north_raw = north_raw.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
                    north_page = re.sub(r"\s+", " ", north_raw).strip().lower()
                except Exception as exc:
                    error = (error + "; " if error else "") + "Northern Region source: " + str(exc)

                # Only update the species requested for repair here. All other
                # species retain their existing rules and results above.
                if north_page:
                    if "closed ocean salmon" in north_page or ("closed fishing seasons" in north_page and "ocean salmon" in north_page):
                        salmon_detail = "CDFW's Northern Region page lists ocean salmon as closed for the North Coast region. This applies to ocean salmon fishing in the listed region; do not use it to infer separate river rules. Coho (silver) salmon are excluded from California's ocean salmon daily bag allowance."
                        species["chinook-salmon"] = {"state": "closed", "label": "RED — Ocean salmon closed in the Northern Region", "detail": salmon_detail, "source": north_url}
                        species["coho-salmon"] = {"state": "closed", "label": "RED — Ocean salmon closed; coho may not be retained", "detail": salmon_detail, "source": north_url}
                    if "pacific sanddab and other flatfish" in north_page and "open year-round" in north_page:
                        species["flounder"] = {"state": "open", "label": "GREEN — Listed flatfish open year-round", "detail": "CDFW lists Pacific sanddab and specified flatfish as open year-round at all depths, including butter sole, curlfin sole, flathead sole, rex sole, rock sole and sand sole. Petrale sole and starry flounder are also listed open year-round. Identify the exact flatfish and check its individual size and bag rules before keeping it.", "source": north_url}
                    if "the recreational fishery for surfperch" in north_page and "open" in north_page:
                        species["surfperch"] = {"state": "open", "label": "GREEN — Surfperch season open; limits apply", "detail": "CDFW lists North Coast surfperch as open. The general daily bag and possession limit is 20 fish combined (except shiner perch), no more than 10 of any one species; redtail surfperch minimum size is 10½ inches total length. Verify the exact current local rule before keeping fish.", "source": north_url}
                    species["anchovies"] = {"state": "check", "label": "YELLOW — Anchovy rule not separately listed", "detail": "CDFW's Northern Region summary does not give a specific recreational rule for anchovies. Do not substitute the Pacific herring rule for anchovies; check the full current regulations for the exact baitfish and collection method.", "source": north_url}
                    species["sardines"] = {"state": "check", "label": "YELLOW — Sardine rule not separately listed", "detail": "CDFW's Northern Region summary does not give a specific recreational rule for sardines. Do not assume Pacific herring limits apply to sardines; verify the exact species and method in the full current regulations.", "source": north_url}
                    species["bat-ray"] = {"state": "check", "label": "YELLOW — Bat ray retention rule not confirmed", "detail": "The Northern Region summary does not state a specific bat ray bag, size or retention rule. This is not confirmation that retention is allowed; check the full California ocean sport-fishing regulations and local marine protected area restrictions before keeping one.", "source": north_url}

                # If CDFW blocks or times out on the server request, retain a clearly labelled
                # fallback based on the official page's published CDFW summary instead
                # of replacing every card with an unhelpful yellow. The message below makes
                # clear that this snapshot could not be freshly verified.
                if not page:
                    def snap(slug, state, label, detail):
                        species[slug] = {"state": state, "label": label, "detail": detail, "source": _BAY_CDFW_URL}
                    snap("chinook-salmon", "closed", "RED — Ocean salmon closed in Klamath Management Zone", "CDFW's page says ocean salmon is closed in the Klamath Management Zone for the remainder of 2026. This is for the named ocean zone only; do not apply it automatically to Humboldt Bay or river fisheries.")
                    snap("coho-salmon", "closed", "RED — Ocean salmon closed in Klamath Management Zone", "The CDFW North Coast summary lists ocean salmon as closed in the Klamath Management Zone for the remainder of 2026. Exact bay and river rules are separate.")
                    snap("california-halibut", "open", "GREEN — Ocean season listed open; limits apply", "CDFW's Oct 9, 2026 summary says California halibut is open year-round. North of Point Sur: 2 fish daily/possession and 22-inch minimum total length. Local bay, gear and MPA restrictions still apply.")
                    snap("flounder", "check", "YELLOW — Identify the exact flatfish", "The CDFW San Francisco Bay District summary lists Pacific sanddab and specified flatfish as open year-round at all depths. Petrale sole and starry flounder are also listed open year-round with no depth restrictions or bag limits. This card groups several flatfish together; identify the exact species and verify its complete size, bag, and other rules before keeping it.")
                    snap("rockfish", "open", "GREEN — Ocean season category listed open", "CDFW lists rockfish under open North Coast ocean fishing seasons. Species, depth, boat/shore rules, bag limits and MPAs can still restrict fishing or retention.")
                    snap("surfperch", "open", "GREEN — Ocean season listed open; limits apply", "CDFW says surfperch is open year-round. Combined daily bag/possession limit is 20 (except shiner perch), with no more than 10 of any one species; redtail surfperch minimum size is 10½ inches.")
                    snap("anchovies", "check", "YELLOW — Anchovy rule not confirmed by this summary", "CDFW specifically lists Pacific herring, not anchovies, in the page text. Do not assume they have the same rules; verify the exact species and method.")
                    snap("sardines", "check", "YELLOW — Sardine rule not confirmed by this summary", "The San Francisco Bay District summary does not give a specific sardine rule in its listed entries. Check the full current ocean sport-fishing rules before targeting or keeping sardines.")
                    snap("bat-ray", "check", "YELLOW — Bat ray rule not specified on this summary", "The CDFW San Francisco Bay District summary does not give a specific recreational take, size, or bag rule for bat rays in the entries checked. This is not confirmation that a bat ray may be kept. Check the complete current California ocean sport fishing regulations and local restrictions before targeting or retaining one.")
                    snap("green-sturgeon", "closed", "RED — Protected; do not take or keep", "CDFW says green sturgeon may not be taken or possessed at any time and must be released immediately without being removed from the water.")
                    snap("white-sturgeon", "check", "YELLOW — Verify the exact waterbody; ocean retention is zero", "The CDFW page states that ocean white sturgeon fishing is catch-and-release only October 1, 2026 through June 30, 2027, with zero daily and annual retention, plus a report card and specific hook/handling rules. Do not apply this ocean rule automatically to every bay, tidal river, or freshwater reach. Verify the exact waterbody and current sturgeon rules.")
                    snap("dungeness-crab", "closed", "RED — Dungeness crab season listed closed", "CDFW's Oct 9, 2026 summary says the season is closed as of July 31 and scheduled to reopen Nov 7, 2026. A scheduled reopening is not a guarantee; check for updates before crabbing.")
                    snap("red-rock-crab", "open", "GREEN — Rock crab season listed open; limits apply", "CDFW says all rock crab species, including red crab, are open year-round statewide, with a 35-crab daily bag limit and 4-inch minimum. Check domoic-acid advisories, gear rules and local restrictions.")

                # Leopard shark: use the Northern Region shark entry, not the generic
                # state-managed shark summary. The official page gives a year-round season,
                # 3 fish daily/possession, and 36-inch minimum size.
                if north_page and "the recreational fishery for leopard shark" in north_page and "open year-round" in north_page:
                    species["leopard-shark"] = {
                        "state": "open",
                        "label": "GREEN — Leopard shark open year-round",
                        "detail": "CDFW lists recreational leopard shark fishing as open year-round at all depths. Daily bag and possession limit: 3 fish. Minimum size: 36 inches total length. Leopard shark may not be taken or possessed in Groundfish Exclusion Areas. Check the exact location, marine protected areas, current in-season notices, and handling/fillet rules before fishing.",
                        "source": north_url,
                    }

                # Final source-specific overrides for the two cards still being corrected:
                # California halibut follows the SF Bay page; surfperch follows Northern Region.
                if page and "the recreational fishery for california halibut" in page and "remains open year-round" in page:
                    species["california-halibut"] = {
                        "state": "open",
                        "label": "GREEN — California halibut open year-round",
                        "detail": "CDFW's SF Bay District page lists California halibut as open year-round. North of Point Sur, the daily bag and possession limit is 2 fish and the minimum size is 22 inches total length. Check the exact water, gear restrictions and marine protected areas.",
                        "source": _BAY_CDFW_URL,
                    }
                if north_page and "the recreational fishery for surfperch" in north_page and "open year-round" in north_page:
                    species["surfperch"] = {
                        "state": "open",
                        "label": "GREEN — Northern Region surfperch open year-round",
                        "detail": "CDFW's Northern Region page lists surfperch as open year-round. The daily bag and possession limit is 20 fish combined across species (except shiner perch), with no more than 10 of any one species. Shiner perch have a separate 20-fish limit. Redtail surfperch minimum size is 10½ inches total length.",
                        "source": north_url,
                    }

                # Northern anchovy and Pacific sardine are open year-round under
                # the general ocean finfish rules, with no numerical bag limit.
                # Keep the Monterey Bay health advisory explicitly scoped to that area.
                baitfish_url = "https://wildlife.ca.gov/Fishing/Ocean/Regulations/Sport-Fishing/General-Ocean-Fishing-Regs"
                baitfish_detail = "CDFW's general ocean finfish regulations state there are no closed seasons for finfish unless a species-specific rule says otherwise, and specifically list northern anchovy and Pacific sardine among species with no numerical bag limit. This applies to Humboldt-area ocean waters unless a local/in-season closure or health advisory applies. Dip nets may be used to take anchovies; check permitted gear, exact waterbody, marine protected areas, and current health advisories. The Monterey Bay anchovy restriction is geographically limited and should not be applied to Humboldt."
                species["anchovies"] = {
                    "state": "open",
                    "label": "GREEN — Northern anchovy open year-round; no bag limit",
                    "detail": baitfish_detail,
                    "source": baitfish_url,
                }
                species["sardines"] = {
                    "state": "open",
                    "label": "GREEN — Pacific sardine open year-round; no bag limit",
                    "detail": baitfish_detail,
                    "source": baitfish_url,
                }

                checked_at = now.strftime("%b %d, %Y %I:%M %p %Z")
                _BAY_REG_CHECK.update({
                    "checked_at": checked_at,
                    "status": "ok" if page else "unavailable",
                    "species": species,
                    "sources": {"san_francisco_bay": _BAY_CDFW_URL},
                    "refresh_interval_hours": 24,
                    "message": ("Live check succeeded against the official CDFW San Francisco Bay District page. Status colours reflect this ocean summary only; bay/river, MPA, gear, size, bag, and health rules may add restrictions." if page else "LIVE CDFW CHECK FAILED. Showing a fallback snapshot based on the CDFW page published Oct 9, 2026; it may be out of date. Open the official page and verify before fishing. The site will retry after 24 hours."),
                    "error": error,
                })
                _BAY_REG_CHECKED_TS = time.time()
    return jsonify(_BAY_REG_CHECK)

@app.route("/map")
def map_page():
    return render_template("map.html")

@app.route("/bay")
def bay_page():
    return render_template("bay.html")


@app.route("/fishing-reports")
def fishing_reports_page():
    return render_template("fishing_reports.html")

@app.route("/weather")
def weather_page():
    return render_template("weather.html")

@app.route("/api/weather")
def weather_api():
    try:
        return jsonify(get_weather())
    except Exception as exc:
        if weather_cache["data"]:
            old = dict(weather_cache["data"])
            old["status"] = "stale"
            old["error"] = str(exc)
            return jsonify(old)
        return jsonify({"status": "unavailable", "error": str(exc), "towns": {}}), 502

@app.route("/api/weather/refresh")
def refresh_weather():
    try:
        return jsonify(get_weather(force=True))
    except Exception as exc:
        return jsonify({"status": "unavailable", "error": str(exc)}), 502

@app.route("/best-days")
def best_days_page():
    """Show calculated fishing windows from the existing local weather, wind and tide feeds."""
    return render_template("best_days.html")

@app.route("/wind")
def wind_page():
    return render_template("wind.html")

@app.route("/api/wind")
def wind_api():
    now = datetime.now(PACIFIC)
    with lock:
        if wind_cache["data"] and wind_cache["fetched"] and (now - wind_cache["fetched"]).total_seconds() < CACHE_SECONDS:
            return jsonify(wind_cache["data"])
        try:
            # Open-Meteo provides an hourly forecast for Humboldt Bay coordinates.
            response = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": 40.75, "longitude": -124.20,
                "current": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
                "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
                "wind_speed_unit": "mph", "timezone": "America/Los_Angeles",
                "forecast_days": 16
            }, timeout=25)
            response.raise_for_status()
            payload = response.json()
            hourly = payload.get("hourly", {})
            current = payload.get("current", {})
            data = {"current": {"time": current.get("time"), "speed": current.get("wind_speed_10m"),
                                "gusts": current.get("wind_gusts_10m"), "direction": current.get("wind_direction_10m")},
                    "times": hourly.get("time", []), "speed": hourly.get("wind_speed_10m", []),
                    "gusts": hourly.get("wind_gusts_10m", []), "direction": hourly.get("wind_direction_10m", []),
                    "updated": now.isoformat(), "status": "live", "source": "Open-Meteo hourly forecast for Humboldt Bay"}
            if not data["times"]: raise RuntimeError("No hourly wind forecast was returned.")
            wind_cache.update(fetched=now, data=data)
            return jsonify(data)
        except Exception as exc:
            if wind_cache["data"]:
                old = dict(wind_cache["data"]); old["status"] = "stale"; old["error"] = str(exc)
                return jsonify(old)
            return jsonify({"status":"unavailable", "error":str(exc), "times":[], "speed":[], "gusts":[], "direction":[]}), 502

@app.route("/api/tides")
def tides_api():
    try:
        return jsonify(get_tides())
    except Exception as exc:
        if cache["data"]:
            old = dict(cache["data"])
            old["status"] = "stale"
            old["error"] = str(exc)
            return jsonify(old)
        return jsonify({"status": "unavailable", "error": str(exc), "predicted": [], "observed": []}), 502

@app.route("/api/tides/refresh")
def refresh_tides():
    try:
        return jsonify(get_tides(force=True))
    except Exception as exc:
        return jsonify({"status": "unavailable", "error": str(exc)}), 502

@app.route("/upload", methods=["GET", "POST"])
def upload_photo():
    if request.method == "POST":
        photo = request.files.get("photo")
        if not photo or not photo.filename:
            flash("Choose a photo before submitting.", "error")
            return redirect(url_for("upload_photo"))
        if not allowed_image(photo.filename):
            flash("Please upload a JPG, PNG, WEBP, or GIF image.", "error")
            return redirect(url_for("upload_photo"))
        data = photo.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            flash("The image must be 8 MB or smaller.", "error")
            return redirect(url_for("upload_photo"))
        # Check the actual file signature rather than trusting its extension or browser MIME type.
        signatures = (data.startswith(b"\xff\xd8\xff"), data.startswith(b"\x89PNG\r\n\x1a\n"), data.startswith(b"RIFF") and data[8:12] == b"WEBP", data.startswith((b"GIF87a", b"GIF89a")))
        if not any(signatures):
            flash("That file does not look like a supported image.", "error")
            return redirect(url_for("upload_photo"))
        ext = secure_filename(photo.filename).rsplit(".", 1)[1].lower()
        filename = f"{secrets.token_hex(18)}.{ext}"
        (UPLOAD_DIR / filename).write_bytes(data)
        original = secure_filename(photo.filename)[:180] or "photo"
        with db_connect() as conn:
            conn.execute("INSERT INTO submissions(filename, original_name, caption, species, location, submitted_at, approved) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (filename, original, request.form.get("caption", "")[:500], request.form.get("species", "")[:100], request.form.get("location", "")[:150], datetime.now(PACIFIC).strftime("%Y-%m-%d %I:%M %p %Z")))
        flash("Thanks! Your photo was submitted for review.", "success")
        return redirect(url_for("upload_photo"))
    return render_template("upload.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_required():
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/analytics")
def admin_analytics():
    if not admin_required():
        return redirect(url_for("admin_login"))
    now = datetime.now().timestamp()
    windows = [("30 minutes", 30 * 60), ("24 hours", 24 * 60 * 60), ("4 days", 4 * 24 * 60 * 60)]
    stats = []
    with db_connect() as conn:
        for label, seconds in windows:
            count = conn.execute("SELECT COUNT(DISTINCT visitor_id) FROM page_views WHERE viewed_at >= ? AND visitor_id IS NOT NULL", (now - seconds,)).fetchone()[0]
            stats.append({"label": label, "count": count})
        recent = conn.execute("SELECT path, COUNT(*) AS views FROM page_views WHERE viewed_at >= ? GROUP BY path ORDER BY views DESC LIMIT 8", (now - 4 * 24 * 60 * 60,)).fetchall()
        # Count unique verified Google email addresses that have successfully signed in.
        conn.execute("CREATE TABLE IF NOT EXISTS google_users (google_sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, last_login TEXT NOT NULL)")
        signed_in_emails = conn.execute("SELECT COUNT(DISTINCT lower(trim(email))) FROM google_users WHERE email IS NOT NULL AND trim(email) != ''").fetchone()[0]
    return render_template("admin_analytics.html", stats=stats, recent=recent, signed_in_emails=signed_in_emails)

@app.route("/admin/photos")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with db_connect() as conn:
        submissions = conn.execute("SELECT * FROM submissions ORDER BY approved ASC, id DESC").fetchall()
    return render_template("admin.html", submissions=submissions)

@app.route("/admin/photo/<int:photo_id>/<action>", methods=["POST"])
def moderate_photo(photo_id, action):
    if not admin_required():
        abort(403)
    if action not in {"approve", "reject", "delete"}:
        abort(404)
    with db_connect() as conn:
        item = conn.execute("SELECT * FROM submissions WHERE id=?", (photo_id,)).fetchone()
        if item is None:
            abort(404)
        if action == "delete":
            (UPLOAD_DIR / item["filename"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM submissions WHERE id=?", (photo_id,))
        elif action == "approve":
            conn.execute("UPDATE submissions SET approved=1 WHERE id=?", (photo_id,))
        else:
            conn.execute("UPDATE submissions SET approved=0 WHERE id=?", (photo_id,))
    flash({"approve": "Photo approved and now public.", "reject": "Photo returned to pending review.", "delete": "Photo deleted."}[action], "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/community-photos")
def community_photos():
    with db_connect() as conn:
        photos = conn.execute("SELECT * FROM submissions WHERE approved=1 ORDER BY id DESC").fetchall()
    return render_template("community_photos.html", photos=photos)

@app.route("/community-photo-file/<path:filename>")
def community_photo_file(filename):
    # Only approved images are served from the private upload folder.
    with db_connect() as conn:
        item = conn.execute("SELECT id FROM submissions WHERE filename=? AND approved=1", (filename,)).fetchone()
    if item is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)

@app.route("/admin-private-photo/<path:filename>")
def admin_private_photo(filename):
    if not admin_required():
        abort(403)
    with db_connect() as conn:
        item = conn.execute("SELECT id FROM submissions WHERE filename=?", (filename,)).fetchone()
    if item is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)

@app.errorhandler(413)
def too_large(_error):
    return "Upload too large. Images must be 8 MB or smaller.", 413


@app.route("/login/google")
def login_google():
    if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET"):
        flash("Google sign-in is not configured yet. Please try again later.", "error")
        return redirect(request.args.get("next") or url_for("fishing_stories") + "#share-story")
    next_url = request.args.get("next") or request.referrer or url_for("fishing_stories") + "#share-story"
    # Only allow same-site relative paths as post-login destinations.
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("fishing_stories") + "#share-story"
    session["google_login_next"] = next_url
    redirect_uri = url_for("login_google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/login/google/callback")
def login_google_callback():
    """Complete Google OpenID Connect login and persist the account in Flask's session."""
    try:
        token = google.authorize_access_token()
        # Authlib normally supplies validated OIDC claims as token["userinfo"].
        # Fetch the UserInfo endpoint as a fallback for providers/configurations
        # that return an ID token without attaching the claims to the token dict.
        user = token.get("userinfo")
        if not user:
            try:
                user = google.userinfo(token=token)
            except Exception:
                user = google.parse_id_token(token)
    except Exception as exc:
        app.logger.exception("Google sign-in callback failed: %s", exc)
        flash("Google sign-in could not be completed. Please try again. If it continues, check the server log.", "error")
        return redirect(url_for("fishing_stories") + "#share-story")

    email_verified = user.get("email_verified") if user else False
    email_verified = email_verified is True or str(email_verified).lower() == "true"
    if not user or not user.get("sub") or not user.get("email") or not email_verified:
        app.logger.warning("Google sign-in returned incomplete or unverified account claims.")
        flash("Google did not provide a verified email address. Please choose a verified Google account.", "error")
        return redirect(url_for("fishing_stories") + "#share-story")

    account = {
        "sub": str(user["sub"]),
        "email": str(user["email"]),
        "name": user.get("name") or user.get("given_name") or "Angler",
        "picture": user.get("picture"),
    }
    session["google_user"] = account
    session.permanent = True

    with db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS google_users (google_sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, last_login TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO google_users(google_sub,email,name,last_login) VALUES(?,?,?,?) "
            "ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,name=excluded.name,last_login=excluded.last_login",
            (account["sub"], account["email"], account["name"], datetime.now(PACIFIC).isoformat()),
        )

    flash("You're signed in with Google.", "success")
    next_url = session.pop("google_login_next", url_for("fishing_stories") + "#share-story")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("fishing_stories") + "#share-story"
    return redirect(next_url)

@app.route("/logout")
def logout():
    session.pop("google_user", None)
    flash("You have been signed out.", "success")
    next_url = request.args.get("next") or request.referrer or url_for("index")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")
    return redirect(next_url)

@app.route("/fishing-stories", methods=["GET", "POST"])
def fishing_stories():
    if request.method == "POST":
        if not session.get("google_user"):
            flash("Please sign in with Google before publishing your story.", "error")
            return redirect(url_for("fishing_stories") + "#share-story")
        title = request.form.get("title", "").strip()[:120]
        author = request.form.get("author", "").strip()[:80] or session.get("google_user", {}).get("name", "Angler")
        body = request.form.get("body", "").strip()[:10000]
        photo = request.files.get("photo")
        if not title or not author or not body:
            flash("Please enter a story title, your name or nickname, and your story.", "error")
            return redirect(url_for("fishing_stories") + "#share-story")
        filename = None
        original = None
        image_url = None
        cloudinary_public_id = None
        if photo and photo.filename:
            if not allowed_image(photo.filename):
                flash("Please upload a JPG, PNG, WEBP, or GIF photo.", "error")
                return redirect(url_for("fishing_stories") + "#share-story")
            data = photo.read(MAX_IMAGE_BYTES + 1)
            signatures = (data.startswith(b"\xff\xd8\xff"), data.startswith(b"\x89PNG\r\n\x1a\n"), data.startswith(b"RIFF") and data[8:12] == b"WEBP", data.startswith((b"GIF87a", b"GIF89a")))
            if len(data) > MAX_IMAGE_BYTES or not any(signatures):
                flash("The photo must be a valid JPG, PNG, WEBP, or GIF image under 8 MB.", "error")
                return redirect(url_for("fishing_stories") + "#share-story")
            original = secure_filename(photo.filename)[:180] or "photo"
            if cloudinary_enabled():
                try:
                    image_url, cloudinary_public_id = upload_story_image(data)
                except Exception:
                    app.logger.exception("Cloudinary story photo upload failed")
                    flash("We couldn't save your photo right now. Please try again in a moment.", "error")
                    return redirect(url_for("fishing_stories") + "#share-story")
            else:
                ext = secure_filename(photo.filename).rsplit(".", 1)[1].lower()
                filename = f"story_{secrets.token_hex(18)}.{ext}"
                (UPLOAD_DIR / filename).write_bytes(data)
        token = secrets.token_urlsafe(32)
        with story_db_connect() as conn:
            if DATABASE_URL:
                conn.execute("INSERT INTO fishing_stories(title, author, body, filename, original_name, image_url, cloudinary_public_id, status_token, submitted_at, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
                    (title, author, body, filename, original, image_url, cloudinary_public_id, token, datetime.now(PACIFIC).strftime("%Y-%m-%d %I:%M %p %Z")))
            else:
                conn.execute("INSERT INTO fishing_stories(title, author, body, filename, original_name, image_url, cloudinary_public_id, status_token, submitted_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (title, author, body, filename, original, image_url, cloudinary_public_id, token, datetime.now(PACIFIC).strftime("%Y-%m-%d %I:%M %p %Z")))
        # The private status link is a bearer link: only someone with this unique URL can check this submission.
        return redirect(url_for("story_status", token=token) + "?submitted=1")
    with story_db_connect() as conn:
        stories = conn.execute("SELECT * FROM fishing_stories WHERE status='published' ORDER BY id DESC").fetchall()
    return render_template("fishing_stories.html", stories=stories)

@app.route("/fishing-story-status/<token>")
def story_status(token):
    with story_db_connect() as conn:
        if DATABASE_URL:
            story = conn.execute("SELECT title, author, status, submitted_at FROM fishing_stories WHERE status_token=%s", (token,)).fetchone()
        else:
            story = conn.execute("SELECT title, author, status, submitted_at FROM fishing_stories WHERE status_token=?", (token,)).fetchone()
    if story is None:
        abort(404)
    return render_template("story_status.html", story=story, token=token)

@app.route("/fishing-story-image/<int:story_id>")
def fishing_story_image(story_id):
    with story_db_connect() as conn:
        if DATABASE_URL:
            story = conn.execute("SELECT filename, image_url FROM fishing_stories WHERE id=%s AND status='published'", (story_id,)).fetchone()
        else:
            story = conn.execute("SELECT filename, image_url FROM fishing_stories WHERE id=? AND status='published'", (story_id,)).fetchone()
    if story is None or not (story["image_url"] or story["filename"]):
        abort(404)
    if story["image_url"]:
        return redirect(story["image_url"])
    return send_from_directory(UPLOAD_DIR, story["filename"], as_attachment=False)

@app.route("/admin", methods=["GET", "POST"])
def story_admin():
    if not admin_required():
        if request.method == "POST":
            password = request.form.get("password", "")
            if secrets.compare_digest(password, ADMIN_PASSWORD):
                session.clear()
                session["is_admin"] = True
                session.permanent = True
                return redirect(url_for("story_admin"))
            flash("Incorrect password.", "error")
        return render_template("story_admin_login.html")
    with story_db_connect() as conn:
        stories = conn.execute("SELECT * FROM fishing_stories ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC").fetchall()
    return render_template("story_admin.html", stories=stories)

@app.route("/admin/story-image/<int:story_id>")
def admin_story_image(story_id):
    if not admin_required():
        abort(403)
    with story_db_connect() as conn:
        if DATABASE_URL:
            story = conn.execute("SELECT filename, image_url FROM fishing_stories WHERE id=%s", (story_id,)).fetchone()
        else:
            story = conn.execute("SELECT filename, image_url FROM fishing_stories WHERE id=?", (story_id,)).fetchone()
    if story is None or not (story["image_url"] or story["filename"]):
        abort(404)
    if story["image_url"]:
        return redirect(story["image_url"])
    return send_from_directory(UPLOAD_DIR, story["filename"], as_attachment=False)

@app.route("/admin/story/<int:story_id>/<action>", methods=["POST"])
def moderate_story(story_id, action):
    if not admin_required():
        abort(403)
    if action not in {"publish", "deny", "delete"}:
        abort(404)
    with story_db_connect() as conn:
        if DATABASE_URL:
            story = conn.execute("SELECT * FROM fishing_stories WHERE id=%s", (story_id,)).fetchone()
            id_placeholder = "%s"
        else:
            story = conn.execute("SELECT * FROM fishing_stories WHERE id=?", (story_id,)).fetchone()
            id_placeholder = "?"
        if story is None:
            abort(404)
        if action == "publish":
            conn.execute(f"UPDATE fishing_stories SET status='published' WHERE id={id_placeholder}", (story_id,))
        elif action == "deny":
            conn.execute(f"UPDATE fishing_stories SET status='denied' WHERE id={id_placeholder}", (story_id,))
        else:
            if story["filename"]:
                (UPLOAD_DIR / story["filename"]).unlink(missing_ok=True)
            if story["cloudinary_public_id"]:
                delete_cloudinary_image(story["cloudinary_public_id"])
            conn.execute(f"DELETE FROM fishing_stories WHERE id={id_placeholder}", (story_id,))
    flash({"publish": "Story published on Fishing Stories.", "deny": "Story denied; it remains private.", "delete": "Story deleted."}[action], "success")
    return redirect(url_for("story_admin"))

# Keep Flask's development server startup after every route has been registered.
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
