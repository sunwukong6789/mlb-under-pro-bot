MLB UNDER WEB BOT

Files:
- app.py
- requirements.txt
- render.yaml

Deploy Render:
1. Upload these files to GitHub repo.
2. Render -> New -> Web Service.
3. Build command: pip install -r requirements.txt
4. Start command: python app.py
5. Add env variables:
   TELEGRAM_BOT_TOKEN = your bot token
   TELEGRAM_CHAT_ID = your chat id
   ALERT_SCORE = 80
   CHECK_EVERY_SECONDS = 60
   AUTO_START = 1

Open the Render URL on iPad to see the website.
