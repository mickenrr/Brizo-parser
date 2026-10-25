#!/usr/bin/env python3
"""app.py — Flask web UI for the Brizo lead pipeline."""

import json
import os
import re
import signal
import subprocess
import threading
import uuid
from flask import Flask, Response, jsonify, redirect, render_template_string, request, session

app = Flask(__name__)
app.secret_key = b'brizo-parser-ui-2026-xK9mPqR7nZ'

PARSER_DIR = os.path.dirname(os.path.abspath(__file__))

# run_id -> {'lines': list, 'events': list, 'done': bool, 'cond': Condition, 'limit': int}
_runs: dict = {}


def _classify(line: str) -> str:
    if '═' in line or 'СТАТИСТИКА' in line: return 'stats'
    if line.lstrip().startswith('─') or 'Лид #' in line: return 'lead'
    if 'СТАРТ' in line: return 'start'
    if 'Перемещён' in line or 'LPR contacts done' in line or '✓ Подходит' in line: return 'ok'
    if 'Отклоняем' in line or '✗ Не подходит' in line: return 'reject'
    if 'ERROR' in line or 'CRITICAL' in line or 'Критическая' in line: return 'error'
    return 'info'


def _parse_final_stats(lines: list) -> dict:
    text = '\n'.join(lines)
    def _g(pat):
        m = re.search(pat, text)
        return int(m.group(1)) if m else 0
    return {
        'total':     _g(r'Обработано всего\s*:\s*(\d+)'),
        'qualified': _g(r'Парсю[^:]*:\s*(\d+)'),
        'no_kd':     _g(r'Нет КД[^:]*:\s*(\d+)'),
        'yard':      _g(r'ярд[^:]*:\s*(\d+)'),
        'low_tax':   _g(r'[Мм]ало налогов[^:]*:\s*(\d+)'),
    }


@app.route('/')
def index():
    return render_template_string(INDEX_HTML,
        email=session.get('email', ''),
        password=session.get('password', ''),
        limit=session.get('limit', 50))


@app.route('/start', methods=['POST'])
def start():
    email    = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    mode     = request.form.get('mode', 'limit')  # 'limit' or 'target'

    # Собираем аргументы командной строки
    cmd_args: list[str] = []
    display_limit = 50  # для шаблона run-страницы

    if mode == 'target':
        try:
            target_n = max(1, min(500, int(request.form.get('target') or 10)))
        except ValueError:
            target_n = 10
        cmd_args += ['--target', str(target_n)]
        display_limit = target_n * 10  # примерная оценка для прогресс-бара

        # Дополнительный лимит входящих лидов (необязательно)
        try:
            lim_raw = request.form.get('limit_with_target', '').strip()
            if lim_raw:
                limit_t = max(1, min(2000, int(lim_raw)))
                cmd_args += ['--limit', str(limit_t)]
                display_limit = limit_t
        except ValueError:
            pass
    else:
        try:
            limit = max(1, min(500, int(request.form.get('limit') or 50)))
        except ValueError:
            limit = 50
        cmd_args += ['--limit', str(limit)]
        display_limit = limit

    session.permanent   = True
    session['email']    = email
    session['password'] = password
    session['limit']    = display_limit

    run_id = uuid.uuid4().hex[:10]
    cond = threading.Condition()
    run = {'lines': [], 'events': [], 'done': False, 'paused': False,
           'proc': None, 'cond': cond, 'limit': display_limit}
    _runs[run_id] = run

    def _worker():
        env = {**os.environ, 'BRIZO_EMAIL': email, 'BRIZO_PASSWORD': password}
        try:
            proc = subprocess.Popen(
                ['python3', 'main.py'] + cmd_args,
                cwd=PARSER_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True, bufsize=1,
                start_new_session=True,
            )
            run['proc'] = proc
            for raw in iter(proc.stdout.readline, ''):
                line = raw.rstrip('\n')
                with cond:
                    if line.startswith('PIPELINE_EVENT:'):
                        run['events'].append(line[len('PIPELINE_EVENT:'):])
                    else:
                        run['lines'].append(line)
                    cond.notify_all()
            proc.wait()
        except Exception as exc:
            with cond:
                run['lines'].append(f'[PIPELINE ERROR] {exc}')
                cond.notify_all()
        finally:
            with cond:
                run['done'] = True
                cond.notify_all()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(f'/run/{run_id}')


@app.route('/run/<run_id>')
def run_page(run_id):
    if run_id not in _runs:
        return redirect('/')
    return render_template_string(RUN_HTML, run_id=run_id, limit=_runs[run_id]['limit'])


def _send_signal(run_id: str, sig: int) -> bool:
    run = _runs.get(run_id)
    proc = run.get('proc') if run else None
    if not proc or run.get('done'):
        return False
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except Exception:
        return False


@app.route('/pause/<run_id>', methods=['POST'])
def pause_run(run_id):
    ok = _send_signal(run_id, signal.SIGSTOP)
    if ok and run_id in _runs:
        _runs[run_id]['paused'] = True
    return jsonify(ok=ok, paused=_runs.get(run_id, {}).get('paused', False))


@app.route('/resume/<run_id>', methods=['POST'])
def resume_run(run_id):
    _send_signal(run_id, signal.SIGCONT)  # на случай ручной паузы через SIGSTOP
    run = _runs.get(run_id)
    proc = run.get('proc') if run else None
    if run_id in _runs:
        _runs[run_id]['paused'] = False
    if proc and run and not run.get('done'):
        try:
            proc.stdin.write('RESUME\n')
            proc.stdin.flush()
        except Exception:
            pass
    return jsonify(ok=True, paused=False)


@app.route('/stop/<run_id>', methods=['POST'])
def stop_run(run_id):
    run = _runs.get(run_id)
    proc = run.get('proc') if run else None
    if proc and not run.get('done'):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGCONT)  # снять паузу, иначе SIGTERM не дойдёт
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        if run_id in _runs:
            _runs[run_id]['paused'] = False
    return jsonify(ok=True)


@app.route('/stream/<run_id>')
def stream(run_id):
    run = _runs.get(run_id)
    if not run:
        return Response('data: {}\n\n', status=404, mimetype='text/event-stream')

    def _generate():
        log_idx = 0
        evt_idx = 0
        cond = run['cond']
        while True:
            with cond:
                while (log_idx >= len(run['lines']) and
                       evt_idx >= len(run['events']) and
                       not run['done']):
                    cond.wait(timeout=20)
                new_logs   = run['lines'][log_idx:]
                new_events = run['events'][evt_idx:]
                is_done    = run['done']

            for line in new_logs:
                log_idx += 1
                yield f"data: {json.dumps({'t': line, 'k': _classify(line)})}\n\n"

            for evt_raw in new_events:
                evt_idx += 1
                yield f"event: pipeline\ndata: {evt_raw}\n\n"

            # Keepalive: если данных не было — посылаем SSE-комментарий чтобы
            # Railway/Nginx не разрывали соединение по idle-таймауту.
            if not new_logs and not new_events and not is_done:
                yield ":\n\n"

            if is_done and log_idx >= len(run['lines']) and evt_idx >= len(run['events']):
                stats = _parse_final_stats(run['lines'])
                yield f"event: done\ndata: {json.dumps(stats)}\n\n"
                break

    return Response(_generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── HTML: login page ──────────────────────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brizo Parser</title>
<style>
:root{
  --bg:#0c1220;--card:#131b2e;--border:#1e2d4a;--text:#c8d6f0;--muted:#5a6e96;
  --accent:#3d8ef8;--accent-h:#2979e8;--input:#0e1828;
}
@media(prefers-color-scheme:light){:root{
  --bg:#f0f4fb;--card:#fff;--border:#dde6f5;--text:#1a2540;--muted:#6b7ea8;
  --accent:#2563eb;--accent-h:#1d4ed8;--input:#f5f8ff;
}}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
}
.card{
  background:var(--card);border:1px solid var(--border);border-radius:16px;
  padding:44px 40px;width:100%;max-width:400px;
  box-shadow:0 8px 48px rgba(0,0,0,.25);
}
.logo{
  width:48px;height:48px;background:var(--accent);border-radius:12px;
  display:flex;align-items:center;justify-content:center;font-size:22px;
  margin-bottom:20px;
}
h1{font-size:22px;font-weight:700;letter-spacing:-.3px}
.sub{color:var(--muted);font-size:13px;margin-top:4px;margin-bottom:32px}
.field{margin-bottom:18px}
label{
  display:block;font-size:11px;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);margin-bottom:7px
}
input{
  width:100%;padding:11px 14px;
  background:var(--input);border:1.5px solid var(--border);
  border-radius:8px;color:var(--text);font-size:14px;outline:none;
  transition:border-color .15s;
}
input:focus{border-color:var(--accent)}
.pw{position:relative}
.pw input{padding-right:44px}
.eye{position:absolute;right:12px;top:50%;transform:translateY(-50%);
     background:none;border:none;cursor:pointer;color:var(--muted);font-size:16px;padding:4px}
.sep{border:none;border-top:1px solid var(--border);margin:24px 0}
.help{font-size:11px;color:var(--muted);margin-top:5px}
.mode-toggle{display:flex;gap:0;border:1.5px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:14px}
.mode-toggle label{
  flex:1;text-align:center;padding:8px 0;font-size:12px;font-weight:700;
  letter-spacing:.04em;text-transform:uppercase;cursor:pointer;
  color:var(--muted);background:var(--input);transition:background .15s,color .15s;
}
.mode-toggle input[type=radio]{display:none}
.mode-toggle input[type=radio]:checked+label{background:var(--accent);color:#fff}
.mode-extra{margin-top:12px}
.btn{
  width:100%;padding:13px;background:var(--accent);color:#fff;
  border:none;border-radius:9px;font-size:15px;font-weight:700;cursor:pointer;
  transition:background .15s;margin-top:8px;display:flex;align-items:center;
  justify-content:center;gap:9px;letter-spacing:.01em;
}
.btn:hover{background:var(--accent-h)}
.btn svg{width:17px;height:17px;fill:none;stroke:#fff;stroke-width:2.5;stroke-linecap:round}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⚡</div>
  <h1>Brizo Parser</h1>
  <div class="sub">Автоквалификация лидов из «База»</div>
  <form method="post" action="/start">
    <div class="field">
      <label>Email Brizo</label>
      <input type="email" name="email" value="{{ email }}" placeholder="user@example.ru" required autocomplete="username">
    </div>
    <div class="field">
      <label>Пароль Brizo</label>
      <div class="pw">
        <input type="password" name="password" id="pw" value="{{ password }}" placeholder="••••••••" required autocomplete="current-password">
        <button type="button" class="eye" onclick="this.previousElementSibling.type=this.previousElementSibling.type==='password'?'text':'password'">👁</button>
      </div>
    </div>
    <hr class="sep">
    <div class="field">
      <label>Режим остановки</label>
      <div class="mode-toggle">
        <input type="radio" name="mode" id="mode-limit" value="limit" checked>
        <label for="mode-limit">Лимит</label>
        <input type="radio" name="mode" id="mode-target" value="target">
        <label for="mode-target">Цель</label>
      </div>

      <div id="block-limit">
        <input type="number" name="limit" id="inp-limit" value="{{ limit }}" min="1" max="500">
        <div class="help">Обработать N лидов из Базы</div>
      </div>

      <div id="block-target" style="display:none">
        <input type="number" name="target" id="inp-target" value="10" min="1" max="500">
        <div class="help">Остановиться после N лидов в Парсю</div>
        <div class="mode-extra">
          <label style="margin-top:12px">Макс. лидов (необяз.)</label>
          <input type="number" name="limit_with_target" id="inp-limit-t" value="" min="1" max="2000" placeholder="без ограничения">
          <div class="help">Дополнительный лимит входящих лидов</div>
        </div>
      </div>
    </div>
    <button type="submit" class="btn">
      <svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"/></svg>
      Запустить
    </button>
  </form>
  <script>
  document.querySelectorAll('input[name=mode]').forEach(r => r.addEventListener('change', function(){
    document.getElementById('block-limit').style.display  = this.value === 'limit'  ? '' : 'none';
    document.getElementById('block-target').style.display = this.value === 'target' ? '' : 'none';
  }));
  </script>
</div>
</body>
</html>"""


# ── HTML: live run page ───────────────────────────────────────────────────────

RUN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brizo Parser — запуск</title>
<style>
:root{
  --bg:#0c1220;--surface:#131b2e;--surface2:#0e1525;--border:#1e2d4a;
  --text:#c8d6f0;--muted:#5a6e96;--faint:#3a4e72;
  --accent:#3d8ef8;--accent-h:#2979e8;--green:#22d49a;--red:#f05252;--yellow:#f5a623;--gray:#3a4e72;
}
@media(prefers-color-scheme:light){:root{
  --bg:#eef2fb;--surface:#fff;--surface2:#f5f8ff;--border:#d5e0f5;
  --text:#1a2540;--muted:#6b7ea8;--faint:#c0cce8;
  --accent:#2563eb;--accent-h:#1d4ed8;--green:#16a34a;--red:#dc2626;--yellow:#d97706;--gray:#94a3b8;
}}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  min-height:100vh;display:flex;flex-direction:column;overflow:hidden;height:100vh;
}

/* ── Header ── */
.hdr{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:0 24px;height:52px;display:flex;align-items:center;gap:14px;flex-shrink:0;
}
.hdr-logo{font-size:18px;font-weight:800;letter-spacing:-.3px;color:var(--text)}
.hdr-logo span{color:var(--accent)}
.hdr-sep{flex:1}
.badge{
  display:flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;
  font-size:12px;font-weight:700;letter-spacing:.03em;
}
.badge.running{background:rgba(61,142,248,.12);color:var(--accent)}
.badge.done{background:rgba(34,212,154,.12);color:var(--green)}
.pulse{
  width:7px;height:7px;border-radius:50%;background:var(--accent);
  animation:pulse 1.4s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}
.back{
  padding:5px 14px;border-radius:8px;border:1px solid var(--border);
  background:none;color:var(--muted);font-size:12px;cursor:pointer;text-decoration:none;
  transition:all .15s;font-weight:600;
}
.back:hover{color:var(--text);border-color:var(--faint)}

/* ── Stats bar ── */
.statsbar{
  background:var(--surface2);border-bottom:1px solid var(--border);
  padding:12px 24px;display:flex;align-items:center;gap:0;flex-shrink:0;
}
.stat{
  display:flex;flex-direction:column;align-items:center;padding:0 20px;
  border-right:1px solid var(--border);
}
.stat:first-child{padding-left:0}
.stat:last-child{border-right:none}
.stat-val{
  font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;line-height:1;
}
.stat-lbl{font-size:11px;color:var(--muted);margin-top:3px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.c-accent .stat-val{color:var(--accent)}
.c-green  .stat-val{color:var(--green)}
.c-red    .stat-val{color:var(--red)}
.c-yellow .stat-val{color:var(--yellow)}
.c-muted  .stat-val{color:var(--muted)}
.progress-wrap{flex:1;padding:0 24px}
.progress-track{
  height:6px;background:var(--border);border-radius:3px;overflow:hidden;
}
.progress-fill{
  height:100%;background:var(--accent);border-radius:3px;width:0%;
  transition:width .4s ease;
}
.progress-pct{font-size:12px;color:var(--muted);margin-top:5px;text-align:right;font-weight:700}

/* ── Body split ── */
.body{flex:1;display:flex;overflow:hidden;min-height:0}

/* ── Leads panel (left) ── */
.leads-panel{
  flex:1 1 0;min-width:0;display:flex;flex-direction:column;
  border-right:1px solid var(--border);overflow:hidden;
}
.panel-hdr{
  padding:12px 20px;border-bottom:1px solid var(--border);
  font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  color:var(--muted);display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;
}
.leads-scroll{flex:1;overflow-y:auto;padding:10px 12px}
.leads-scroll::-webkit-scrollbar{width:4px}
.leads-scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── Lead row ── */
.lead-row{
  display:flex;align-items:center;gap:10px;
  padding:9px 10px;border-radius:8px;margin-bottom:3px;
  transition:background .15s;
}
.lead-row:hover{background:var(--surface)}
.dot{
  width:10px;height:10px;border-radius:50%;flex-shrink:0;
}
.dot.pending{background:var(--border)}
.dot.processing{
  background:var(--yellow);
  animation:pulse 1s ease-in-out infinite;
}
.dot.qualified{background:var(--green)}
.dot.site_not_found{background:var(--yellow)}
.dot.rejected{background:var(--red)}
.dot.error{background:var(--gray)}
.lead-num{
  font-size:11px;color:var(--muted);font-weight:700;
  font-variant-numeric:tabular-nums;min-width:36px;flex-shrink:0;
  font-family:monospace;
}
.lead-name{font-size:13px;font-weight:600;flex:1;min-width:0;
           white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lead-status{
  font-size:11px;font-weight:700;padding:2px 8px;border-radius:12px;
  flex-shrink:0;white-space:nowrap;
}
.status-qualified{background:rgba(34,212,154,.12);color:var(--green)}
.status-site_not_found{background:rgba(245,166,35,.12);color:var(--yellow)}
.status-rejected{background:rgba(240,82,82,.12);color:var(--red)}
.status-error{background:rgba(58,78,114,.2);color:var(--muted)}
.status-processing{background:rgba(245,166,35,.12);color:var(--yellow)}

/* ── Summary table (shown at end) ── */
.summary{display:none;flex-direction:column;gap:0}
.summary-hdr{
  font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  color:var(--muted);padding:10px 20px;border-bottom:1px solid var(--border);
}
.sum-table{flex:1;overflow-y:auto}
.sum-row{
  display:grid;grid-template-columns:40px 1fr 130px;
  gap:0;padding:10px 20px;border-bottom:1px solid var(--border);
  align-items:center;font-size:13px;
}
.sum-row:hover{background:var(--surface2)}
.sum-n{color:var(--muted);font-family:monospace;font-size:12px;font-weight:700}
.sum-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 12px}
.sum-reason{font-size:11px;color:var(--muted);text-align:right}

/* ── Log sidebar (right) ── */
.log-panel{
  width:38%;flex-shrink:0;display:flex;flex-direction:column;
  background:var(--surface2);overflow:hidden;
}
.log-scroll{
  flex:1;overflow-y:auto;padding:8px 0;
  font-family:'JetBrains Mono',Consolas,'Courier New',monospace;
  font-size:11.5px;line-height:1.6;
}
.log-scroll::-webkit-scrollbar{width:3px}
.log-scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.log-line{padding:0 14px;white-space:pre-wrap;word-break:break-all;color:var(--muted)}
.log-line.ok{color:var(--green)}
.log-line.reject{color:var(--red)}
.log-line.error{color:var(--red);font-weight:700}
.log-line.stats{color:var(--yellow)}
.log-line.start{color:var(--accent);font-weight:700}
.log-line.lead{color:var(--text);font-weight:700;margin-top:4px}
.log-scroll-btn{
  padding:5px 12px;border-top:1px solid var(--border);
  font-size:11px;color:var(--muted);display:flex;align-items:center;
  justify-content:space-between;flex-shrink:0;
}
.scroll-toggle{
  background:none;border:1px solid var(--border);border-radius:4px;
  color:var(--muted);font-size:10px;padding:2px 8px;cursor:pointer;
  transition:all .15s;
}
.scroll-toggle:hover{color:var(--text);border-color:var(--faint)}

/* ── Captcha modal ── */
.modal-overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.72);
  z-index:2000;
  align-items:center;justify-content:center;
  padding:24px;
}
.modal-overlay.visible{display:flex}
.modal-card{
  background:#fff;
  color:#1a2540;
  border-radius:20px;
  padding:40px 44px;
  max-width:520px;
  width:100%;
  box-shadow:0 32px 80px rgba(0,0,0,.45);
  text-align:center;
  position:relative;
}
@media(prefers-color-scheme:dark){
  .modal-card{background:#1c2640;color:#c8d6f0}
  .modal-steps{background:#131b2e}
  .modal-step-num{color:#3d8ef8}
}
.modal-icon{font-size:56px;line-height:1;margin-bottom:16px}
.modal-title{
  font-size:20px;font-weight:800;
  color:#d97706;
  margin-bottom:10px;letter-spacing:-.2px;
}
.modal-subtitle{
  font-size:14px;line-height:1.6;
  color:#5a6e96;
  margin-bottom:24px;
}
.modal-steps{
  background:#f5f8ff;
  border-radius:12px;
  padding:18px 22px;
  margin-bottom:28px;
  text-align:left;
}
.modal-step{
  display:flex;align-items:flex-start;gap:12px;
  font-size:14px;line-height:1.6;
  color:#1a2540;
}
.modal-step + .modal-step{margin-top:10px}
.modal-step-num{
  font-size:13px;font-weight:800;
  color:#2563eb;
  background:rgba(37,99,235,.1);
  border-radius:50%;
  width:24px;height:24px;
  display:flex;align-items:center;justify-content:center;
  flex-shrink:0;margin-top:1px;
}
.modal-step-txt strong{color:#1a2540}
.modal-btn{
  display:inline-flex;align-items:center;gap:8px;
  background:#16a34a;color:#fff;
  border:none;border-radius:10px;
  padding:14px 36px;
  font-size:15px;font-weight:700;
  cursor:pointer;
  transition:background .15s,transform .1s;
  letter-spacing:.01em;
}
.modal-btn:hover{background:#15803d;transform:translateY(-1px)}
.modal-btn:active{transform:translateY(0)}
</style>
</head>
<body>

<!-- Captcha modal -->
<div id="captchaOverlay" class="modal-overlay" onclick="return false">
  <div class="modal-card">
    <div class="modal-icon">⚠️</div>
    <div class="modal-title">Требуется действие</div>
    <div class="modal-subtitle">
      Checko обнаружил подозрительную активность и просит<br>подтвердить, что вы человек.
    </div>
    <div class="modal-steps">
      <div class="modal-step">
        <div class="modal-step-num">1</div>
        <div class="modal-step-txt">Откройте <strong>checko.ru</strong> в браузере</div>
      </div>
      <div class="modal-step">
        <div class="modal-step-num">2</div>
        <div class="modal-step-txt">Нажмите галочку <strong>«Я не робот»</strong> и кнопку <strong>«Подтвердить»</strong></div>
      </div>
      <div class="modal-step">
        <div class="modal-step-num">3</div>
        <div class="modal-step-txt">Вернитесь сюда и нажмите кнопку ниже</div>
      </div>
    </div>
    <button class="modal-btn" onclick="resumeAfterCaptcha()">✅ Я прошёл капчу — продолжить</button>
  </div>
</div>

<!-- Header -->
<div class="hdr">
  <div class="hdr-logo">Brizo<span>Parser</span></div>
  <div class="hdr-sep"></div>
  <div class="badge running" id="badge"><div class="pulse" id="pulse"></div><span id="badgeTxt">В процессе</span></div>
  <button id="btnPause" onclick="togglePause()" class="back" style="margin-left:12px">⏸ Пауза</button>
  <button id="btnStop"  onclick="stopRun()"    class="back" style="margin-left:6px;color:var(--red);border-color:var(--red)">⏹ Стоп</button>
  <a href="/" class="back" style="margin-left:10px">← Новый запуск</a>
</div>

<!-- Stats bar -->
<div class="statsbar">
  <div class="stat c-accent">
    <div class="stat-val" id="sv-processed">0 / {{ limit }}</div>
    <div class="stat-lbl">Обработано</div>
  </div>
  <div class="progress-wrap">
    <div class="progress-track"><div class="progress-fill" id="progress"></div></div>
    <div class="progress-pct" id="progress-pct">0%</div>
  </div>
  <div class="stat c-green">
    <div class="stat-val" id="sv-qualified">0</div>
    <div class="stat-lbl">В Парсю</div>
  </div>
  <div class="stat c-red">
    <div class="stat-val" id="sv-rejected">0</div>
    <div class="stat-lbl">Проиграно</div>
  </div>
  <div class="stat c-yellow">
    <div class="stat-val" id="sv-site-not-found">0</div>
    <div class="stat-lbl">Сайт не найден</div>
  </div>
  <div class="stat c-muted">
    <div class="stat-val" id="sv-remaining">{{ limit }}</div>
    <div class="stat-lbl">Осталось</div>
  </div>
</div>

<!-- Body -->
<div class="body">

  <!-- Leads panel -->
  <div class="leads-panel">
    <div class="panel-hdr">
      <span>Сделки</span>
      <span id="lead-count" style="color:var(--faint)">0</span>
    </div>
    <div class="leads-scroll" id="leadsEl"></div>
  </div>

  <!-- Log sidebar -->
  <div class="log-panel">
    <div class="panel-hdr">
      <span>Логи</span>
      <span id="log-count" style="color:var(--faint)">0 строк</span>
    </div>
    <div class="log-scroll" id="logEl"></div>
    <div class="log-scroll-btn">
      <span style="font-size:10px;color:var(--faint)" id="log-status">ожидание...</span>
      <button class="scroll-toggle" id="scrollBtn" onclick="toggleScroll()">⬇ авто</button>
    </div>
  </div>

</div>

<script>
const RUN_ID = '{{ run_id }}';
const LIMIT  = {{ limit }};

let isPaused = false;
let isStopped = false;

async function togglePause() {
  if (isStopped) return;
  const endpoint = isPaused ? 'resume' : 'pause';
  const r = await fetch(`/${endpoint}/${RUN_ID}`, {method: 'POST'});
  const d = await r.json();
  isPaused = d.paused;
  _updateControls();
}

async function stopRun() {
  if (isStopped) return;
  if (!confirm('Остановить парсинг?')) return;
  await fetch(`/stop/${RUN_ID}`, {method: 'POST'});
  isStopped = true;
  _updateControls();
}

function _updateControls() {
  const pulse = document.getElementById('pulse');
  if (isStopped) {
    badge.className = 'badge done';
    badgeTxt.textContent = 'Остановлено';
    if (pulse) pulse.style.display = 'none';
    btnPause.disabled = true;
    btnStop.disabled  = true;
    btnPause.style.opacity = '0.4';
    btnStop.style.opacity  = '0.4';
  } else if (isPaused) {
    badge.className = 'badge';
    badge.style.background = 'rgba(245,166,35,.15)';
    badge.style.color = 'var(--yellow)';
    badgeTxt.textContent = 'На паузе';
    if (pulse) pulse.style.display = 'none';
    btnPause.textContent = '▶ Продолжить';
  } else {
    badge.className = 'badge running';
    badge.style.background = '';
    badge.style.color = '';
    badgeTxt.textContent = 'В процессе';
    if (pulse) pulse.style.display = '';
    btnPause.textContent = '⏸ Пауза';
  }
}

const btnPause = document.getElementById('btnPause');
const btnStop  = document.getElementById('btnStop');

let processed = 0, qualified = 0, rejected = 0, siteNotFound = 0, totalFound = LIMIT;
let autoScroll = true;
let logCount = 0;
let leadCount = 0;

// elements
const leadsEl  = document.getElementById('leadsEl');
const logEl    = document.getElementById('logEl');
const svProc   = document.getElementById('sv-processed');
const svQual   = document.getElementById('sv-qualified');
const svSNF    = document.getElementById('sv-site-not-found');
const svRej    = document.getElementById('sv-rejected');
const svRem    = document.getElementById('sv-remaining');
const progFill = document.getElementById('progress');
const progPct  = document.getElementById('progress-pct');
const badge    = document.getElementById('badge');
const badgeTxt = document.getElementById('badgeTxt');

function toggleScroll() {
  autoScroll = !autoScroll;
  document.getElementById('scrollBtn').textContent = autoScroll ? '⬇ авто' : '— стоп';
}

logEl.addEventListener('scroll', () => {
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 40;
  if (!atBottom) autoScroll = false;
});

function updateStats(q, r, e, snf) {
  if (q   !== undefined) qualified   = q;
  if (r   !== undefined) rejected    = r;
  if (snf !== undefined) siteNotFound = snf;
  processed = qualified + rejected + siteNotFound + (e || 0);
  const pct = totalFound > 0 ? Math.round(processed / totalFound * 100) : 0;
  const rem = Math.max(0, totalFound - processed);
  svProc.textContent  = `${processed} / ${totalFound}`;
  svQual.textContent  = qualified;
  svRej.textContent   = rejected;
  svSNF.textContent   = siteNotFound;
  svRem.textContent   = rem;
  progFill.style.width = pct + '%';
  progPct.textContent  = pct + '%';
  document.getElementById('lead-count').textContent = leadCount;
}

// lead card map: num → element
const leadCards = {};

function addLeadCard(num, name, status, reason) {
  leadCount++;
  const row = document.createElement('div');
  row.className = 'lead-row';
  row.id = 'lead-' + num;

  const dot = document.createElement('div');
  dot.className = 'dot ' + status;

  const numEl = document.createElement('div');
  numEl.className = 'lead-num';
  numEl.textContent = String(num).padStart(2,'0');

  const nameEl = document.createElement('div');
  nameEl.className = 'lead-name';
  nameEl.textContent = name;

  const stEl = document.createElement('div');
  stEl.className = 'lead-status status-' + status;
  stEl.textContent = statusLabel(status, reason);

  row.appendChild(dot);
  row.appendChild(numEl);
  row.appendChild(nameEl);
  row.appendChild(stEl);
  leadsEl.appendChild(row);
  leadCards[num] = {row, dot, stEl};

  // auto-scroll leads list
  leadsEl.scrollTop = leadsEl.scrollHeight;
  return row;
}

function updateLeadCard(num, status, reason) {
  const card = leadCards[num];
  if (!card) return;
  card.dot.className  = 'dot ' + status;
  card.stEl.className = 'lead-status status-' + status;
  card.stEl.textContent = statusLabel(status, reason);
}

function statusLabel(status, reason) {
  if (status === 'qualified')      return '→ Парсю';
  if (status === 'site_not_found') return '→ База ❓';
  if (status === 'processing')     return '…';
  if (status === 'error')          return reason || 'Ошибка';
  return reason || 'Проиграно';
}

// Log line
function appendLog(text, kind) {
  const div = document.createElement('div');
  div.className = 'log-line ' + (kind || 'info');
  div.textContent = text;
  logEl.appendChild(div);
  logCount++;
  document.getElementById('log-count').textContent = logCount + ' строк';
  if (autoScroll) logEl.scrollTop = logEl.scrollHeight;
}

// SSE
const es = new EventSource('/stream/' + RUN_ID);

es.onmessage = function(e) {
  const d = JSON.parse(e.data);
  appendLog(d.t, d.k || 'info');
  document.getElementById('log-status').textContent = 'работает…';
};

es.addEventListener('pipeline', function(e) {
  console.log('[SSE] pipeline событие получено:', e.data);
  let evt;
  try {
    evt = JSON.parse(e.data);
  } catch(err) {
    console.error('[SSE] ошибка парсинга JSON:', err, e.data);
    return;
  }

  if (evt.type === 'total') {
    totalFound = evt.total;
    svRem.textContent = totalFound;
    svProc.textContent = `0 / ${totalFound}`;
  }

  if (evt.type === 'lead_start') {
    addLeadCard(evt.num, evt.name, 'processing', '');
  }

  if (evt.type === 'lead_done') {
    updateLeadCard(evt.num, evt.status, evt.reason);
    updateStats(evt.qualified, evt.rejected, evt.errors, evt.site_not_found);
  }

  if (evt.type === 'captcha_detected') {
    isPaused = true;
    _updateControls();
    document.getElementById('captchaOverlay').classList.add('visible');
  }
});

async function resumeAfterCaptcha() {
  document.getElementById('captchaOverlay').classList.remove('visible');
  const r = await fetch(`/resume/${RUN_ID}`, {method: 'POST'});
  const d = await r.json();
  isPaused = d.paused;
  _updateControls();
}

es.addEventListener('done', function(e) {
  es.close();
  isStopped = true;
  const s = JSON.parse(e.data);
  badge.className = 'badge done';
  badge.style.background = '';
  badge.style.color = '';
  badgeTxt.textContent = 'Завершено';
  document.title = 'Brizo Parser — готово';
  document.getElementById('log-status').textContent = 'завершено';
  document.getElementById('pulse').style.display = 'none';
  btnPause.disabled = true;
  btnStop.disabled  = true;
  btnPause.style.opacity = '0.4';
  btnStop.style.opacity  = '0.4';
  if (s.total) {
    svProc.textContent = `${s.total} / ${totalFound}`;
    progFill.style.width = '100%';
    progPct.textContent = '100%';
    svRem.textContent = '0';
  }
});

es.onerror = function() {
  if (es.readyState === EventSource.CLOSED) return;
  appendLog('[СОЕДИНЕНИЕ ПРЕРВАНО]', 'error');
  document.getElementById('log-status').textContent = 'ошибка соединения';
};
</script>
</body>
</html>"""


if __name__ == '__main__':
    port = int(os.getenv("PORT", "8080"))
    print(f'Brizo Parser UI → http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
