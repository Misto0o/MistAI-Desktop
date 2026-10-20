"""A run_command route that needs credentials the agent doesn't have gets one
general NOTE after its RESULT (config.credential_failure_reason, AgentLoop).

No service-specific logic: the patterns are generic HTTP/auth wording and
placeholder credentials. Withheld output (secret guard) is never examined.
"""

import pytest

from core.agent import CREDENTIAL_NOTE
from core.config import credential_failure_reason
from test_input_repeat_and_done_check import _result, _run

PS5_401 = ("COMMAND FAILED (exit code 1).\nSTDERR: Invoke-RestMethod : The remote server "
           "returned an error: (401) Unauthorized.")
PS7_401 = ("COMMAND FAILED (exit code 1).\nSTDERR: Invoke-RestMethod: Response status code "
           "does not indicate success: 401 (Unauthorized).")
PS_403 = ("COMMAND FAILED (exit code 1).\nSTDERR: Invoke-WebRequest : The remote server "
          "returned an error: (403) Forbidden.")
CURL_401_BODY = ('COMMAND COMPLETED SUCCESSFULLY (exit code 0).\nSTDOUT: {"cod":401, "message": '
                 '"Invalid API key. Please see the documentation for more info."}')


@pytest.mark.parametrize("output", [
    PS5_401, PS7_401, PS_403, CURL_401_BODY,
    "STDERR: authentication failed for this request",
    'STDOUT: {"error": "API key is missing"}',
    'STDOUT: {"error": "missing access token"}',
], ids=["ps5-401", "ps7-401", "403-forbidden", "curl-exit0-json-body", "auth-failed",
        "key-missing", "token-missing"])
def test_credential_failures_in_the_output(output):
    assert credential_failure_reason("Invoke-RestMethod https://api.example.com/v1/data",
                                     output)


@pytest.mark.parametrize("command", [
    "curl.exe 'https://api.example.com/v1/data?q=paris&appid=YOUR_API_KEY'",
    "Invoke-RestMethod 'https://api.example.com/v1/data?key={API_KEY}'",
    "curl.exe -H 'Authorization: Bearer <your_token>' https://api.example.com/v1/me",
    "curl.exe 'https://api.example.com/v1/data?appid=YOURKEY'",
    "curl.exe 'https://api.example.com/v1/data?token=XXXXXXXX'",
])
def test_placeholder_credentials_in_the_command(command):
    # Even when the command "succeeded" with ordinary output.
    assert credential_failure_reason(command, "COMMAND COMPLETED SUCCESSFULLY (exit code 0).") \
        == "the command contains a placeholder credential"


@pytest.mark.parametrize("command, output", [
    ("Get-Content rooms.txt", "STDOUT: Room 401  Building A\nRoom 402  Building A"),
    ("netstat -ano", "STDOUT: TCP    0.0.0.0:403    0.0.0.0:0    LISTENING    4\nport 403 open"),
    ("Invoke-WebRequest https://example.com/missing",
     "COMMAND FAILED (exit code 1).\nSTDERR: The remote server returned an error: (404) Not Found."),
    ("Invoke-RestMethod https://api.example.com/v1/data",
     'STDOUT: {"temp": 12.5, "status": "ok", "count": 401}'),
    ("Get-ChildItem", "STDOUT: Forbidden City.jpg\nunauthorized_access_policy.txt"),
    ("Get-Content $env:APPDATA\\app\\settings.json | Select-Object -First 3",
     "STDOUT: {\"theme\": \"dark\"}"),
    ("Get-Date", ""),
], ids=["room-401", "port-403", "plain-404", "normal-api-response", "words-without-codes",
        "env-var-not-a-placeholder", "no-output"])
def test_ordinary_output_never_fires(command, output):
    assert credential_failure_reason(command, output) is None


def test_withheld_output_is_never_examined():
    # The caller passes no output when it was withheld; nothing else fires.
    assert credential_failure_reason("Get-Content .env", "") is None


# -- the agent loop -------------------------------------------------------------------

DONE = {"action": "done", "speech": "Done."}


def _results(**details):
    return {action: _result(action, detail=detail) for action, detail in details.items()}


def test_the_note_follows_a_placeholder_url_and_only_run_command_gets_it(monkeypatch, capsys):
    page = "Sign in to see this page. Invalid API key? Contact support. (401) Unauthorized"
    out, _, calls, obs, _ = _run(monkeypatch, [
        {"action": "run_command",
         "parameter": "curl.exe 'https://api.example.com/v1/data?appid=YOUR_API_KEY'"},
        {"action": "browser_get_text", "parameter": ""},
        {"action": "run_command", "parameter": "Get-Date"},
        DONE,
    ], results=_results(run_command=CURL_401_BODY, browser_get_text=page))
    assert out["success"]
    assert CREDENTIAL_NOTE in obs[0]
    assert obs[0].index("RESULT:") < obs[0].index(CREDENTIAL_NOTE)       # after RESULT
    assert CREDENTIAL_NOTE not in obs[1]                                 # not a browser read
    assert "[AgentLoop] Credential hint: the command contains a placeholder credential" \
        in capsys.readouterr().out


def test_a_plain_command_gets_no_note(monkeypatch):
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "run_command", "parameter": "Get-ChildItem"},
        DONE,
    ], results=_results(run_command="COMMAND COMPLETED SUCCESSFULLY (exit code 0).\n"
                                    "STDOUT: Room 401  Building A"))
    assert CREDENTIAL_NOTE not in obs[0]


def test_the_note_survives_the_result_cut(monkeypatch):
    long_output = "COMMAND FAILED (exit code 1).\nSTDOUT: " + "x" * 9000 + "\n" + PS5_401
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "run_command", "parameter": "Invoke-RestMethod https://api.example.com/v1"},
        DONE,
    ], results=_results(run_command=long_output))
    assert "[truncated," in obs[0] and "(401) Unauthorized" not in obs[0]   # cut away...
    assert obs[0].rstrip().endswith(CREDENTIAL_NOTE)                     # ...note kept


def test_withheld_output_gets_no_note(monkeypatch):
    withheld = ("COMMAND FAILED (exit code 1).\nOUTPUT WITHHELD: command references "
                "secret-pattern file '.env'. Invalid API key (401) Unauthorized")
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "run_command", "parameter": "Get-Content .env"},
        DONE,
    ], results=_results(run_command=withheld))
    assert CREDENTIAL_NOTE not in obs[0]


def test_the_note_has_no_service_specific_wording():
    lowered = CREDENTIAL_NOTE.lower()
    assert not any(w in lowered for w in ("weather", "openweather", "forecast", "appid"))
