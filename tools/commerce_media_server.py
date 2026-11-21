"""Read-only image service for a public tunnel. Never serves the application or directories."""
import argparse
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit


def serve(root,port):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):self.send_image(False)
        def do_GET(self):self.send_image(True)
        def send_image(self,body):
            key=urlsplit(self.path).path.lstrip('/')
            if not re.fullmatch(r'[a-f0-9]{64}\.jpg',key):self.send_error(404);return
            path=root/key
            if not path.is_file():self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(path.stat().st_size))
            self.send_header('Cache-Control','public, max-age=86400, immutable');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
            if body:
                with path.open('rb') as f:
                    while chunk:=f.read(65536):self.wfile.write(chunk)
        def log_message(self,fmt,*args):
            print(self.log_date_time_string(),fmt%args,flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--port',type=int,default=8010);args=p.parse_args()
    serve(Path(args.root).resolve(),args.port)
