Humboldt Fishers — Unique Device Analytics

This version adds a long-lived first-party cookie to recognize the same browser on repeat page loads. The analytics dashboard counts distinct browser IDs during each time window, so 20 refreshes from the same browser count as one device in that window.

Limitations: cookie clearing, private browsing, different browsers, or different devices can be counted separately. This is not a guaranteed person count. Previously recorded views without device IDs are not included in unique-device totals.

Run from Command Prompt in the project folder: py -m pip install -r requirements.txt && py app.py
Open http://127.0.0.1:5000/admin and click Analytics.
