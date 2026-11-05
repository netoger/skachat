#!/usr/bin/python3
import sys
import site
from pathlib import Path

backend = Path(__file__).resolve().parent / 'app'
site.addsitedir(str(backend / '.venv/lib/python3.10/site-packages'))
sys.path.insert(0, str(backend))
from webhook_cgi import main

main()
