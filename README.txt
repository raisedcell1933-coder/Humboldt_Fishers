HUMBOLDT FISHERS

Run locally from Command Prompt in this folder:
  py -m pip install -r requirements.txt
  py app.py
Then open http://127.0.0.1:5000

FISHING STORIES
Public story page: http://127.0.0.1:5000/fishing-stories
Private admin sign-in: http://127.0.0.1:5000/admin
Admin password: Set securely using the HUMBOLDT_FISHERS_ADMIN_PASSWORD environment variable.

Visitors can submit a story title, author/nickname, story text and optional photo. New submissions stay private until published by the admin. The submitter receives a private status link showing whether their submission is pending, published, or denied. Only published stories and photos appear on the public Fishing Stories page.

IMPORTANT BEFORE PUBLIC HOSTING
Set HUMBOLDT_FISHERS_ADMIN_PASSWORD to a strong password and HUMBOLDT_FISHERS_SECRET_KEY to a long random secret in your hosting provider's environment settings. The requested password is included as a default for local testing, but should not be used as the public production password. Ensure the host has persistent disk storage for the SQLite database and private_uploads folder. Use HTTPS when publicly hosted.

The weather and tide pages use external live data services and need an internet connection.

Weekly tide reset: the charts use the current Monday-Sunday week until Sunday at 2:00 AM Pacific. At or after 2:00 AM Sunday, the page switches to the next Monday-Sunday week. Each daily graph and table covers 4:00 AM through 2:00 AM the following day. Actual water-level measurements naturally remain blank until NOAA reports observations for the new week. This schedule is calculated on every page load and does not depend on saved browser storage.


WEEKLY RIVER HEIGHT & FLOW CHARTS
The Rivers page now includes Monday-Sunday height (feet) and flow (CFS) charts using the NOAA National Water Prediction Service gauge API where data is available. The page refreshes data hourly and rolls the chart window to the next Monday-Sunday week at 2:00 AM Pacific on Sunday. Some gauges may not return forecasts or both measurements; unavailable data is left blank and the official gauge link is provided. The Mendocino Coast Streams card uses the Noyo River near Fort Bragg as a clearly labeled representative gauge, not as a measurement of every coastal stream.

Official data/API documentation: https://api.water.noaa.gov/nwps/v1/docs/

River chart parser fix: NWPS stageflow uses primary/secondary values per dated record; this version maps primary to stage and secondary to flow. If a gauge does not publish a metric or forecast, that series can still be empty.


MOON CYCLE DATA SOURCE UPDATE
The Moon Cycle page now requests a 14-day range from SunriseSunset.io (no API key required) and no longer requires PyEphem. The page shows sunrise/sunset bearings, moonrise/moonset times, moon phase, and illumination. SunriseSunset.io attribution is included in the page footer. This API does not provide moonrise/moonset bearings, so the page does not display invented values for those. An internet connection is required when the page loads.


GOOGLE SIGN-IN SETUP (required before public story submissions)
1. In Google Cloud Console, create/select a project and configure the OAuth consent screen.
2. Create an OAuth Client ID of type "Web application".
3. Add your deployed site origin to Authorized JavaScript origins:
   https://fishhumboldt.com
4. Add this Authorized redirect URI:
   https://fishhumboldt.com/login/google/callback
5. In Render Environment, set:
   GOOGLE_CLIENT_ID = your Google OAuth client ID
   GOOGLE_CLIENT_SECRET = your Google OAuth client secret
   HUMBOLDT_FISHERS_SECRET_KEY = a long random secret
   HUMBOLDT_FISHERS_ADMIN_PASSWORD = a strong private admin password
   COOKIE_SECURE = true
6. If you use a Render temporary URL before connecting the domain, add its origin and
   exact /login/google/callback URL to the Google OAuth client too.
7. Do not put Google client secrets in source code or upload them to GitHub.

Google sign-in verifies the Google account's email_verified claim. The website does
not ask for or store users' Google passwords. Story submissions require a signed-in
Google account. Existing admin login remains separate.
