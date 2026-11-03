"""Public notice with operator details supplied only by private configuration."""
from dataclasses import dataclass
from html import escape
import os
from urllib.parse import quote


@dataclass(frozen=True)
class OperatorDetails:
    name: str = ""
    location: str = ""
    email: str = ""

    @classmethod
    def from_env(cls):
        return cls(*(os.environ.get(key, "").strip() for key in (
            "POSTTRAIN_OPERATOR_NAME", "POSTTRAIN_OPERATOR_LOCATION", "POSTTRAIN_OPERATOR_EMAIL")))


def _duration(seconds):
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hours"
    if seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def render_legal(operator=None, *, cookie_name="posttrain_session", session_ttl_seconds=43200,
                 login_window_seconds=300):
    """Return self-contained HTML; no stats, files, or implicit environment reads."""
    operator = operator if operator is not None else OperatorDetails()
    if operator.name or operator.location or operator.email:
        contact = "<br>".join(escape(value, quote=True) for value in (operator.name, operator.location) if value)
        if operator.email:
            email = escape(operator.email, quote=True)
            href = escape("mailto:" + quote(operator.email, safe="@+.-"), quote=True)
            contact += ("<br>" if contact else "") + f'<a href="{href}">{email}</a>'
        rights_contact = "Contact the operator using the details above"
    else:
        contact = "Operator and contact details have not been configured for this deployment."
        rights_contact = "Contact the operator once contact details have been configured"
    cookie, ttl, window = escape(cookie_name), _duration(session_ttl_seconds), _duration(login_window_seconds)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Legal &amp; Privacy · Posttrain</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f5f4;color:#292524;font:14px/1.8 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
a{{color:#31595c;text-underline-offset:3px}}a:focus-visible{{outline:2px solid #31595c;outline-offset:4px}}.top{{max-width:1000px;margin:auto;padding:28px 30px;display:flex;align-items:center;justify-content:space-between;gap:20px}}.brand{{font-size:25px;font-weight:650;letter-spacing:-1.2px;text-decoration:none;color:#292524}}.top nav{{font-size:12px}}main{{max-width:850px;margin:12px auto 48px;padding:0 24px}}article{{background:white;border:1px solid #e7e5e4;border-radius:12px;padding:38px 42px}}.eyebrow{{font-size:9px;font-weight:650;letter-spacing:1.5px;color:#ae7644;margin:0 0 12px}}h1{{font-size:32px;line-height:1.2;font-weight:550;letter-spacing:-1px;margin:0 0 16px}}h2{{font-size:17px;font-weight:600;line-height:1.5;margin:29px 0 10px}}p{{font-size:13px;color:#57534e;margin:10px 0}}.intro{{color:#78716c}}code{{font:12px ui-monospace,monospace;background:#f5f5f4;border-radius:3px;padding:2px 4px}}.bottom{{border-top:1px solid #e7e5e4;margin-top:30px;padding-top:18px;font-size:12px}}@media(max-width:560px){{.top{{padding:22px 24px}}main{{padding:0 17px;margin-top:0}}article{{padding:28px 24px}}h1{{font-size:28px}}}}
</style></head><body>
<header class="top"><a class="brand" href="/login">posttrain</a><nav aria-label="Page navigation"><a href="/">Dashboard</a> · <a href="/login">Sign in</a></nav></header>
<main><article><p class="eyebrow">POSTTRAIN / INFORMATION</p><h1>Legal &amp; Privacy</h1>
<p class="intro">Information about this private, noncommercial training-metrics monitor and the data needed to operate it.</p>
<h2>Operator / Offenlegung (§ 25 MedienG)</h2><p>{contact}</p>
<p>This service displays aggregate statistics from a local model-training run. It provides shared-password, read-only access; there is no administrator account or training-control interface.</p>
<h2>Access and essential cookie</h2>
<p>The operator processes connection information, including IP addresses, login attempts and session identifiers, to provide and protect this service. The stated basis is the legitimate interest in securely operating this private monitor (Article 6(1)(f) GDPR).</p>
<p>The application sets one essential first-party login cookie, <code>{cookie}</code>, for {ttl}. It is HttpOnly and SameSite=Strict, and Secure on the public HTTPS hostname. Removing it in your browser requires signing in again. No application analytics, advertising or tracking cookies are configured.</p>
<p>A salted shared-password hash and the session-signing secret are kept in private local configuration, outside the public source repository. No personal visitor account or username is required.</p>
<h2>Storage and retention</h2>
<p>Signed session identifiers map to expiring records in server memory. Logout revokes the session. Expired records are removed when checked or during a later login; inactive records can remain in memory until later activity. Login-attempt counters support a rolling {window} rate-limit window and are pruned on later login attempts. Both session records and rate-limit counters are cleared when the application restarts.</p>
<p>Training and validation history is reconstructed from local allowlisted aggregate events. The dashboard does not set a time-based deletion period for those source aggregates. Resource observations are kept locally in a rolling file containing at most 10,000 observations; the oldest observations roll off as new ones arrive. This file survives application restarts until observations roll off or the operator removes it.</p>
<p>The website shares only authenticated aggregate training and resource statistics. It does not read or serve chat messages, private training logs, aliases, model weights or optimizer/RNG payloads. Passwords and statistics payloads are not logged by the application. HTTP access logging is disabled; warning/error and service or tunnel diagnostics may remain in the host journal under the host's retention settings, rather than an application-defined retention period.</p>
<h2>Cloudflare Tunnel</h2>
<p>This deployment uses Cloudflare Tunnel for public HTTPS access. Cloudflare handles connection data as an infrastructure provider and may process it outside the EEA under its applicable safeguards. Its own processing and retention are described in the <a href="https://www.cloudflare.com/privacypolicy/" rel="noreferrer">Cloudflare privacy policy</a>.</p>
<h2>Your choices and rights</h2>
<p>Connection data and the login cookie are needed for authenticated access. The application performs no visitor profiling or automated decision-making.</p>
<p>{rights_contact} for requests concerning access, correction, erasure, restriction or portability, where applicable, or to object to processing based on legitimate interests. Complaints can be addressed to the <a href="https://www.dsb.gv.at/" rel="noreferrer">Austrian Data Protection Authority</a>.</p>
<p class="bottom"><a href="/">Back to dashboard</a> · <a href="/login">Sign in</a></p>
</article></main></body></html>'''
