#!/usr/bin/python3
import sys
import site

site.addsitedir('/home/c/cc250576/skachat/.venv/lib/python3.10/site-packages')
sys.path.insert(0, '/home/c/cc250576/skachat')
from webhook_cgi import main

main()
