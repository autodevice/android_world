#!/usr/bin/env python3
"""
Simple HTTP server to serve the debug UI and test run data.
Usage: python serve_debug_ui.py [--port PORT] [--test-runs-dir DIR]
"""

import argparse
import http.server
import json
import os
import socketserver
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs


class DebugUIHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, test_runs_dir=None, **kwargs):
        self.test_runs_dir = Path(test_runs_dir) if test_runs_dir else Path.cwd() / "test_runs"
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = unquote(parsed_path.path)
        query = parsed_path.query

        if path == '/' or path == '/debug_ui.html' or path == '/index.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html_path = Path(__file__).parent / 'debug_ui.html'
            if html_path.exists():
                with open(html_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b'<h1>debug_ui.html not found</h1>')
            return

        # API endpoints
        if path == '/api/directories':
            self.serve_directories()
            return

        if path == '/api/planner':
            self.serve_json_data('planner_steps.json', query)
            return

        if path == '/api/executor':
            self.serve_json_data('executor_sessions.json', query)
            return

        if path.startswith('/screenshots/'):
            self.serve_screenshot(path[len('/screenshots/'):], query)
            return

        if path.startswith('/test_runs/'):
            file_path = self.test_runs_dir / path[11:]
            if file_path.exists() and file_path.is_file():
                self.send_response(200)
                if file_path.suffix == '.json':
                    self.send_header('Content-type', 'application/json')
                elif file_path.suffix in ['.png', '.jpg', '.jpeg']:
                    self.send_header('Content-type', f'image/{file_path.suffix[1:]}')
                else:
                    self.send_header('Content-type', 'application/octet-stream')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'File not found')
            return

        super().do_GET()

    def serve_directories(self):
        """List all test run directories with their metadata."""
        entries = []
        if self.test_runs_dir.exists():
            for run_dir in sorted(self.test_runs_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if run_dir.is_dir() and not run_dir.name.startswith('.'):
                    has_logs = (
                        (run_dir / 'planner_steps.json').exists() or
                        (run_dir / 'executor_sessions.json').exists()
                    )
                    if has_logs:
                        stat = run_dir.stat()
                        mtime = datetime.fromtimestamp(stat.st_mtime)
                        
                        metadata_path = run_dir / 'metadata.json'
                        goal = ''
                        success = False
                        if metadata_path.exists():
                            try:
                                with open(metadata_path) as f:
                                    metadata = json.load(f)
                                    goal = metadata.get('goal', '')
                                    success = metadata.get('success', False)
                            except:
                                pass
                        
                        entries.append({
                            'name': run_dir.name,
                            'type': 'directory',
                            'mtime': mtime.isoformat(),
                            'mtime_display': mtime.strftime('%Y-%m-%d %H:%M:%S'),
                            'goal': goal,
                            'success': success
                        })

        content = json.dumps(entries)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content.encode())

    def serve_json_data(self, filename, query):
        """Serve JSON file from test run directory."""
        params = parse_qs(query)
        dir_name = params.get('dir', [None])[0]
        
        if not dir_name:
            self.send_error(400, 'Missing dir parameter')
            return
        
        file_path = self.test_runs_dir / dir_name / filename
        if file_path.exists():
            try:
                with open(file_path, 'rb') as f:
                    content = f.read()
                # Validate JSON
                json.loads(content)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            except (json.JSONDecodeError, Exception) as e:
                self.send_error(500, f'Invalid JSON: {str(e)}')
        else:
            self.send_error(404, f'File not found: {filename}')

    def serve_screenshot(self, filename, query):
        """Serve screenshot from test run directory."""
        params = parse_qs(query)
        dir_name = params.get('dir', [None])[0]
        
        if not dir_name:
            self.send_error(400, 'Missing dir parameter')
            return
        
        screenshot_path = self.test_runs_dir / dir_name / 'screenshots' / filename
        if not screenshot_path.exists():
            screenshot_path = self.test_runs_dir / dir_name / filename
        
        if screenshot_path.exists():
            content_type = 'image/png' if filename.endswith('.png') else 'image/jpeg'
            with open(screenshot_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, f'Screenshot not found: {filename}')

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description='Serve Android World Debug UI')
    parser.add_argument('--port', type=int, default=8000, help='Port to serve on (default: 8000)')
    parser.add_argument('--test-runs-dir', type=str, default=None,
                       help='Directory containing test runs (default: ./test_runs)')
    args = parser.parse_args()

    test_runs_dir = Path(args.test_runs_dir) if args.test_runs_dir else Path.cwd() / "test_runs"

    handler = lambda *args, **kwargs: DebugUIHandler(*args, test_runs_dir=test_runs_dir, **kwargs)

    with socketserver.TCPServer(("", args.port), handler) as httpd:
        print(f"🚀 Debug UI server running at http://localhost:{args.port}/")
        print(f"📁 Serving test runs from: {test_runs_dir}")
        print(f"💡 Open http://localhost:{args.port}/ in your browser")
        print("Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 Server stopped")


if __name__ == '__main__':
    main()

