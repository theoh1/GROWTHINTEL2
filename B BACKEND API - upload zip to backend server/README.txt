BACKEND API ZIP

Upload backend-api.zip to your backend/server host.
Then unzip it on the server.

After unzipping, install requirements:
pip install -r requirements.txt

Then start backend:
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
