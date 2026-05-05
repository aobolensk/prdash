# prdash

A personal dashboard for tracking GitHub pull requests.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add GitHub OAuth credentials
python manage.py migrate
python manage.py runserver
open http://localhost:8000
```
