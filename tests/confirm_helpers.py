"""Give a stub api the REAL Api.ask_confirmation (Yes/No prompt), fed by the
stub's own answers: voice -> its _listen_once, typed -> its _get_text_answer,
submitted the way the page's text box does."""

import threading

from core.api import Api


def bind_confirmation(api):
    for name, value in (("_answer_lock", threading.Lock()),
                        ("_answer_event", threading.Event()),
                        ("_awaiting_text_answer", threading.Event()),
                        ("_pending_answer", None), ("_pending_source", None),
                        ("_voice_start", lambda *_a, **_k: None),
                        ("_voice_mark", lambda *_a, **_k: None),
                        ("CONFIRM_LISTEN_TIMEOUT", 10), ("CONFIRM_TEXT_TIMEOUT", 0)):
        if not hasattr(api, name):
            setattr(api, name, value)
    api.notified = getattr(api, "notified", [])

    def ask(question, mode):
        listen = getattr(api, "_listen_once", None)

        def notify(js):
            api.notified.append(js.split("(", 1)[0])
            if js.startswith("showConfirmPrompt(") and mode != "voice":
                answer = api._get_text_answer(question, timeout=60)
                if answer is not None:
                    Api.submit_answer(api, answer)

        api._notify_js = notify
        if listen is not None:
            api._listen_once = lambda timeout=10, stop_event=None, **_k: listen(timeout=timeout)
        try:
            return Api.ask_confirmation(api, question, mode)
        finally:
            if listen is not None:
                api._listen_once = listen

    api.ask_confirmation = ask
    return api
