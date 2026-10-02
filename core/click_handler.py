"""
core/click_handler.py — unified OCR -> UIA -> Vision fallback click resolution.
"""

import difflib
import time

import pyautogui

try:
    import cv2
    import numpy as np
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

try:
    import dxcam
    DXCAM_AVAILABLE = True
except Exception:
    DXCAM_AVAILABLE = False

_dxcam_camera = None


def _get_dxcam_camera():
    """Lazy singleton -- creating a DXGI duplication device is comparatively
    expensive; grabbing a frame from an existing one is cheap. One camera,
    reused for the life of the process."""
    global _dxcam_camera
    if _dxcam_camera is None and DXCAM_AVAILABLE:
        try:
            _dxcam_camera = dxcam.create(output_idx=0)
        except Exception as e:
            print(f"[OCR] dxcam camera creation failed: {e}")
    return _dxcam_camera

try:
    from pywinauto import Desktop
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False

# YouTube/web UI text (Subscribe, Like, small toolbar labels) is small,
# anti-aliased, and often low-contrast -- exactly the case tesseract is worst
# at out of the box. Two fixes, applied only when the first plain pass comes
# up empty (cheap path stays cheap for the common case of big clear text):
#   1. Upscale before OCR -- tesseract's accuracy on UI-sized fonts (~12-14px)
#      is dramatically better once text is ~2-3x that size.
#   2. Binarize (Otsu threshold) -- turns soft anti-aliased edges into crisp
#      black/white, which is what tesseract's segmenter expects.
OCR_UPSCALE = 2.5
MAX_OCR_DIMENSION = 4000  # hard cap on the upscaled image's longest side
OCR_MIN_CONF = 25       # was 40 -- too strict for small anti-aliased UI text
OCR_MATCH_SCORE = 65    # slightly loosened alongside the lower conf floor
OCR_PSM_MODES = ("--oem 3 --psm 11",)  # trimmed from 2 modes -- wasn't finding
                                        # anything psm 11 didn't, and was
                                        # roughly doubling every OCR call's time


class CoordinateValidator:
    @staticmethod
    def is_valid(x: int, y: int, screen_width: int, screen_height: int) -> bool:
        return 0 <= x <= screen_width and 0 <= y <= screen_height and x > 10 and y > 10


# subscribe->subscribed is the SAME button in its other toggle state (wanted
# match). subscribe->subscriber/subscription is a DIFFERENT UI element that
# happens to share a root word (not wanted) -- and both are just one
# character away from the query, so edit-distance alone can't tell them
# apart. Suffix-list disambiguates: this is deliberately narrow rather than
# a general grammar rule, because guessing wrong here is exactly the bug
# this fixes.
_TOGGLE_SUFFIXES = ("d", "ed")               # subscribe->subscribed, like->liked
_DERIVED_WORD_SUFFIXES = ("r", "rs", "s", "ion", "ions", "ing", "er", "ers")


def _match_score(candidate_lower: str, query_lower: str) -> int:
    """Fuzzy match score, 0-100.

    Two failure modes fixed here:
    - Short-fragment false positives: 'be' is a substring of 'subscribe',
      so naive substring/ratio checks score it ~85 -- a stray OCR fragment
      reading just "BE" would win the click.
    - Shared-root false positives: 'subscribe' is a literal prefix of
      'subscribers' (add "rs") -- character similarity alone can't
      distinguish that from 'subscribe'->'subscribed' (add "d"), which is
      the SAME button in its other toggle state and a match we genuinely
      want. One is the button; the other is the subscriber-count text next
      to it. Suffix-listed instead of guessed."""
    if candidate_lower == query_lower:
        return 100

    if candidate_lower.startswith(query_lower) and len(candidate_lower) > len(query_lower):
        suffix = candidate_lower[len(query_lower):]
        if suffix in _TOGGLE_SUFFIXES:
            return 90
        if suffix in _DERIVED_WORD_SUFFIXES:
            return 0  # different word wearing the same root -- not a match
        # unrecognized short suffix (likely OCR noise) -- fall through

    if abs(len(candidate_lower) - len(query_lower)) > 1:
        return 0  # too different in length to plausibly be the same word
    return int(difflib.SequenceMatcher(None, candidate_lower, query_lower).ratio() * 100)


class ImprovedClickHandler:
    def __init__(self, live_state, vision_engine, screen_buffer):
        self.live = live_state
        self.vision = vision_engine
        self.screen = screen_buffer
        self.last_coords = None
        self.last_method = None
        self.last_query = ""            # what we searched for (for verification)
        self.last_found_text = ""       # the actual word tesseract/UIA matched

    def click_on_text(self, query: str) -> bool:
        """Find and click text: OCR -> UIA -> Vision"""
        print(f"[Click] Searching for: '{query}'")
        self.last_query = query

        coords = self._try_ocr_click(query)
        if coords:
            self.last_method = "OCR"
            return self._execute_click(coords)

        coords = self._try_uia_click(query)
        if coords:
            self.last_method = "UIA"
            return self._execute_click(coords)

        if self.vision.ready:
            coords = self._try_vision_click(query)
            if coords:
                self.last_method = "Vision"
                self.last_found_text = query  # Vision doesn't give us the exact word
                return self._execute_click(coords)

        print(f"[Click] FAILED: Could not find '{query}'")
        return False

    def click_nth_element(self, item_type: str, n: int = 1) -> bool:
        """Find and click Nth element - OCR/UIA first, Vision last"""
        print(f"[Click] Looking for #{n} {item_type}")
        self.last_query = item_type

        coords = self._try_ocr_nth(item_type, n)
        if coords:
            self.last_method = "OCR"
            return self._execute_click(coords)

        coords = self._try_uia_nth(item_type, n)
        if coords:
            self.last_method = "UIA"
            return self._execute_click(coords)

        if self.vision.ready:
            coords = self._try_vision_nth(item_type, n)
            if coords:
                self.last_method = "Vision"
                return self._execute_click(coords)

        print(f"[Click] FAILED: Could not find #{n} {item_type}")
        return False

    def recheck_text_near_last_click(self, radius=60):
        """Re-run OCR in a small box around the last click point, right now.
        Used by verify.py to sanity-check toggle-style clicks (Subscribe ->
        Subscribed): if the exact same word is still sitting in the exact
        same spot immediately after a click that claimed success, that's a
        real (if soft) signal the click didn't land. Returns the best word
        found in that region, or "" if OCR unavailable / nothing found."""
        if not OCR_AVAILABLE or not self.last_coords:
            return ""
        try:
            cv_frame = self._get_frame_for_ocr()
            if cv_frame is None:
                return ""
            x, y = self.last_coords
            h, w = cv_frame.shape[:2]
            l, t = max(0, x - radius), max(0, y - radius)
            r, b = min(w, x + radius), min(h, y + radius)
            crop = cv_frame[t:b, l:r]
            if crop.size == 0:
                return ""
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            text = pytesseract.image_to_string(gray, config="--oem 3 --psm 7").strip()
            return text
        except Exception as e:
            print(f"[OCR] Recheck error: {e}")
            return ""

    def _get_mistai_window_rect(self):
        """Find MistAI's own window rect so we can exclude it from OCR/click
        search -- otherwise the agent can find and click its own echoed task
        text, mistaking its own UI for a real target."""
        try:
            import win32gui
            results = []
            def _cb(h, _):
                if win32gui.IsWindowVisible(h) and "MistAI" in win32gui.GetWindowText(h):
                    results.append(h)
                return True
            win32gui.EnumWindows(_cb, None)
            if results:
                rect = win32gui.GetWindowRect(results[0])
                print(f"[OCR] MistAI-window mask rect: {rect} "
                     f"(title: '{win32gui.GetWindowText(results[0])}')")
                return rect
        except Exception as e:
            print(f"[OCR] MistAI-window rect lookup failed: {e}")
        return None

    def _sane_mask_rect(self, rect, frame_shape):
        """GetWindowRect and the actual captured pixels can disagree badly
        under Windows DPI scaling (a very common setup) -- the window rect
        comes back in a different coordinate space than the screenshot, so
        the 'exclude MistAI's own window' blackout can end up positioned or
        sized WRONG, silently blacking out real page content (like, say,
        exactly where a Subscribe button happens to sit). Refuse to apply a
        mask that covers an implausible fraction of the frame -- better to
        risk MistAI clicking its own UI once (rare) than to systematically
        blind OCR to a chunk of every single search (constant, and much
        harder to diagnose from the outside)."""
        if not rect:
            return None
        l, t, r, b = rect
        h, w = frame_shape[:2]
        area_frac = max(0, (r - l)) * max(0, (b - t)) / max(1, w * h)
        if area_frac > 0.35:
            print(f"[OCR] Mask rect covers {area_frac:.0%} of the frame -- "
                 f"that's implausible for one small window (likely a DPI "
                 f"scaling mismatch between GetWindowRect and the actual "
                 f"capture). Skipping the mask rather than blacking out "
                 f"real content.")
            return None
        return rect

    def _try_uia_click(self, query: str):
        try:
            if not PYWINAUTO_AVAILABLE:
                return None
            desktop = Desktop(backend="uia")
            fw = self.live.snapshot.get("foreground_window", "")
            if not fw or fw == "Unknown":
                return None

            target_window = None
            for w in desktop.windows():
                try:
                    if fw.lower()[:25] in w.window_text().lower():
                        target_window = w
                        break
                except Exception:
                    continue
            if not target_window:
                return None

            try:
                descendants = target_window.descendants(depth=8)
            except RecursionError:
                return None

            ql = query.lower()
            best_match = None
            best_score = 0
            for element in descendants:
                try:
                    name = element.window_text().strip()
                    if not name:
                        continue
                    nl = name.lower()
                    score = _match_score(nl, ql)
                    if score >= 60 and score > best_score:
                        try:
                            rect = element.rectangle()
                            if rect.width > 0 and rect.height > 0:
                                best_score = score
                                best_match = element
                        except Exception:
                            pass
                except Exception:
                    pass

            if best_match:
                try:
                    rect = best_match.rectangle()
                    x = (rect.left + rect.right) // 2
                    y = (rect.top + rect.bottom) // 2
                    if CoordinateValidator.is_valid(x, y, *pyautogui.size()):
                        found_text = best_match.window_text().strip()
                        print(f"[UIA] Found '{found_text}' @ ({x}, {y})")
                        self.last_found_text = found_text
                        return (x, y)
                except Exception:
                    pass
            return None
        except Exception as e:
            print(f"[UIA] Error: {e}")
            return None

    def _try_uia_nth(self, item_type: str, n: int):
        try:
            if not PYWINAUTO_AVAILABLE:
                return None
            desktop = Desktop(backend="uia")
            fw = self.live.snapshot.get("foreground_window", "")
            if not fw or fw == "Unknown":
                return None

            target_window = None
            for w in desktop.windows():
                try:
                    if fw.lower()[:25] in w.window_text().lower():
                        target_window = w
                        break
                except Exception:
                    continue
            if not target_window:
                return None

            try:
                descendants = target_window.descendants(depth=8)
            except RecursionError:
                return None

            matches = []
            item_lower = item_type.lower()
            for element in descendants:
                try:
                    ctrl_type = element.element_info.control_type
                    if item_lower in ctrl_type.lower():
                        rect = element.rectangle()
                        if rect.width > 0 and rect.height > 0:
                            cx = (rect.left + rect.right) // 2
                            cy = (rect.top + rect.bottom) // 2
                            matches.append({"name": element.window_text().strip(), "coords": (cx, cy)})
                except Exception:
                    pass

            if len(matches) >= n:
                x, y = matches[n - 1]["coords"]
                if CoordinateValidator.is_valid(x, y, *pyautogui.size()):
                    print(f"[UIA] Found #{n} {item_type} @ ({x}, {y})")
                    return (x, y)
            return None
        except Exception as e:
            print(f"[UIA] Nth error: {e}")
            return None

    def _try_vision_click(self, query: str):
        try:
            frame = self.screen.frame
            if frame is None:
                print("[Vision] ScreenBuffer.frame is None -- Vision has "
                     "nothing to look at, coordinates would be pure guesses")
                return None
            sw, sh = pyautogui.size()
            coords = self.vision.find_element_coords(query, frame)
            if coords:
                x, y = coords
                if CoordinateValidator.is_valid(x, y, sw, sh):
                    print(f"[Vision] Found '{query}' @ ({x}, {y})")
                    return (x, y)
            return None
        except Exception as e:
            print(f"[Vision] Error: {e}")
            return None

    def _try_vision_nth(self, item_type: str, n: int):
        try:
            frame = self.screen.frame
            if frame is None:
                return None
            sw, sh = pyautogui.size()
            title, coords = self.vision.find_nth_element_coords(item_type, n, frame)
            if coords:
                x, y = coords
                if CoordinateValidator.is_valid(x, y, sw, sh):
                    print(f"[Vision] Found #{n} {item_type} '{title}' @ ({x}, {y})")
                    return (x, y)
            return None
        except Exception as e:
            print(f"[Vision] Nth error: {e}")
            return None

    # -- OCR -------------------------------------------------------------
    #
    # Two bugs fixed here vs the original pass:
    #   1. No upscaling/binarization -- small anti-aliased UI text (YouTube's
    #      "Subscribe" button included) frequently scored under the old
    #      conf<40 cutoff on a raw grayscale frame, so OCR silently found
    #      nothing and the caller fell through to UIA (dead in a browser)
    #      and then Vision, which is why 'Subscribe' worked but felt slow
    #      and unreliable.
    #   2. Single PSM mode -- --psm 11 (sparse text) sometimes misses text
    #      sitting inside a tight button; --psm 6 (uniform block) catches
    #      what 11 misses and vice versa, so both are tried.

    def _ocr_pass(self, gray, query, config):
        """One OCR attempt at a given preprocessing stage. Returns
        (coords, found_word, score, seen_words) -- seen_words is every word
        that passed the confidence floor, with its position, regardless of
        whether it matched. That list is what lets us tell 'this frame is
        blank' apart from 'this frame has text but the target word is
        mysteriously never among it' -- the second case is what a bad mask
        rect blacking out the real button would look like."""
        data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT,
                                         config=config)
        ql = query.lower()
        best_match, best_word, best_score = None, "", 0
        seen_words = []
        for i, word in enumerate(data["text"]):
            if not word.strip() or len(word) < 2:
                continue
            try:
                conf = int(data["conf"][i])
            except (ValueError, TypeError):
                continue
            if conf < OCR_MIN_CONF:
                continue
            x = data["left"][i] + data["width"][i] // 2
            y = data["top"][i] + data["height"][i] // 2
            seen_words.append((word, x, y))
            wl = word.lower()
            score = _match_score(wl, ql)
            if score >= OCR_MATCH_SCORE and score > best_score:
                best_score = score
                best_word = word
                best_match = (x, y)
        return best_match, best_word, best_score, seen_words

    def _prepare_frames(self, cv_frame):
        """Yields (gray_image, scale_factor) attempts, cheapest first. Later
        stages only run if the caller's earlier attempts found nothing."""
        rect = self._get_mistai_window_rect()

        def masked(img):
            safe_rect = self._sane_mask_rect(rect, img.shape)
            if safe_rect:
                l, t, r, b = safe_rect
                hh, ww = img.shape[:2]
                l, t, r, b = max(l, 0), max(t, 0), min(r, ww), min(b, hh)
                img[t:b, l:r] = 0
            img[:80, :] = 0  # browser tab bar / window chrome -- never a target
            return img

        gray = masked(cv2.cvtColor(cv_frame, cv2.COLOR_BGR2GRAY))
        yield gray, 1.0

        # Stage 2: upscale + Otsu binarize -- catches small/low-contrast UI text.
        # Capped so a wide multi-monitor capture (e.g. 5760px across three
        # screens) can't blow up into a 14000px+ image that takes forever to
        # OCR -- the whole point of upscaling is legible small text, not an
        # unbounded blow-up.
        h, w = gray.shape[:2]
        scale = min(OCR_UPSCALE, MAX_OCR_DIMENSION / max(h, w))
        up = cv2.resize(gray, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_CUBIC)
        _, thresh = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield thresh, scale

    def _get_frame_for_ocr(self):
        """dxcam (Desktop Duplication API / DXGI) captures the actual
        GPU-composited output -- the same layer OBS and game-capture tools
        read from. pyautogui.screenshot() goes through the old GDI BitBlt
        path, which is well-documented as blind to hardware-accelerated
        content: Firefox/Chrome's video player, when GPU-decoded, renders
        through a compositing surface GDI simply doesn't see. That's
        consistent with every failure logged so far -- OCR reliably reads
        the tab bar and the (non-accelerated) sidebar thumbnails, and
        reliably finds NOTHING in the video/channel-header area where the
        Subscribe button lives, across multiple different videos and
        sessions. dxcam reads the real composited desktop, so it doesn't
        have this blind spot. Falls back to the old path if dxcam isn't
        installed or a grab comes back empty."""
        if DXCAM_AVAILABLE:
            cam = _get_dxcam_camera()
            if cam is not None:
                try:
                    frame = cam.grab()  # RGB, or None if no new frame yet
                    if frame is not None:
                        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                except Exception as e:
                    print(f"[OCR] dxcam grab failed ({e}) -- falling back")

        try:
            shot = pyautogui.screenshot()
            return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        except Exception as e:
            print(f"[OCR] Direct pyautogui capture failed ({e}) -- "
                 f"falling back to ScreenBuffer")
        try:
            cv_frame = self.screen.cv_frame
            if cv_frame is not None:
                return cv_frame
        except Exception as e:
            print(f"[OCR] ScreenBuffer.cv_frame also failed: {e}")
        return None

    # -- Button-region detection ------------------------------------------
    #
    # Scanning the WHOLE page for one small button's text means "Subscribe"
    # has to win against every thumbnail, title, and sidebar item on a
    # visually dense page like YouTube. Isolating button-SHAPED regions
    # first (Canny edges -> contours -> aspect-ratio filter), then OCRing
    # only inside each small cropped region with per-region contrast
    # enhancement, is a fundamentally more reliable strategy for exactly
    # this kind of compact UI text -- less noise to compete with, and the
    # enhancement is tuned to that one button's actual local contrast
    # instead of one global pass over the entire screen.

    def _find_button_regions(self, cv_frame):
        gray = cv2.cvtColor(cv_frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.bitwise_or(cv2.Canny(gray, 30, 100), cv2.Canny(gray, 100, 200))
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        regions = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w < 60 or w > 600 or h < 20 or h > 120:
                continue
            aspect = w / h
            if aspect < 1.2 or aspect > 10:
                continue
            regions.append((x, y, w, h))
        return regions

    def _ocr_button_regions(self, cv_frame, query):
        """OCR just inside each candidate button region, not the whole
        frame. Returns (coords, word, score) for the best match."""
        regions = self._find_button_regions(cv_frame)
        if not regions:
            return None, "", 0, 0

        best_match, best_word, best_score = None, "", 0
        for (x, y, w, h) in regions:
            crop = cv_frame[y:y + h, x:x + w]
            if crop.size == 0:
                continue
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if np.mean(g) < 128:
                g = cv2.bitwise_not(g)  # light text on dark button
            g = cv2.resize(g, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)
            g = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY, 11, 2)
            text = pytesseract.image_to_string(
                g, config="--oem 3 --psm 7 -c tessedit_char_whitelist="
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz "
            ).strip()
            if not text or len(text) < 2:
                continue
            score = _match_score(text.lower(), query.lower())
            if score > best_score:
                best_score = score
                best_word = text
                best_match = (x + w // 2, y + h // 2)
        return best_match, best_word, best_score, len(regions)

    def _try_ocr_click(self, query: str):
        try:
            if not OCR_AVAILABLE:
                print("[OCR] cv2/pytesseract not available -- skipping OCR entirely")
                return None
            cv_frame = self._get_frame_for_ocr()
            if cv_frame is None:
                return None
            sw, sh = pyautogui.size()
            t0 = time.time()

            # Strategy 1: button-region detection (fast, isolates candidates
            # from page clutter -- try this first).
            match, word, score, n_regions = self._ocr_button_regions(cv_frame, query)
            if match and score >= OCR_MATCH_SCORE:
                x, y = match
                if CoordinateValidator.is_valid(x, y, sw, sh):
                    print(f"[OCR] Found '{query}' as '{word}' @ ({x}, {y}) via "
                          f"button-region detection [score={score}, "
                          f"{n_regions} candidates, {time.time()-t0:.2f}s]")
                    self.last_found_text = word
                    return (x, y)
            if n_regions:
                print(f"[OCR] Button-region pass: {n_regions} candidate shapes, "
                     f"best '{word}' @ score {score} -- trying whole-frame OCR next")

            best_overall = (0, "")
            all_seen = []
            for stage_i, (gray, scale) in enumerate(self._prepare_frames(cv_frame)):
                stage_words = 0
                for config in OCR_PSM_MODES:
                    match, word, score, seen = self._ocr_pass(gray, query, config)
                    stage_words += len(seen)
                    if stage_i == 0:  # only the raw (unscaled) pass has real coords
                        all_seen.extend(seen)
                    if score > best_overall[0]:
                        best_overall = (score, word)
                    if match:
                        x, y = int(match[0] / scale), int(match[1] / scale)
                        if CoordinateValidator.is_valid(x, y, sw, sh):
                            print(f"[OCR] Found '{query}' as '{word}' @ ({x}, {y}) "
                                  f"[scale={scale}, score={score}, "
                                  f"{time.time()-t0:.2f}s]")
                            self.last_found_text = word
                            return (x, y)
                if stage_i == 0 and stage_words == 0:
                    # The RAW frame produced not one single OCR word -- that's
                    # not "text present but no match", that's "this frame has
                    # nothing readable in it" (wrong monitor, blank capture,
                    # stale buffer). Upscaling a blank image just burns
                    # several seconds for zero chance of a different answer,
                    # which is exactly what made this take ~5 minutes across
                    # several retries. Skip straight to Vision instead.
                    print(f"[OCR] Raw frame produced ZERO words at all "
                          f"(frame shape {gray.shape}) -- likely a blank/stale "
                          f"capture, not a matching problem. Skipping the "
                          f"expensive upscale pass and falling through "
                          f"[{time.time()-t0:.2f}s]")
                    return None

            # Forensic dump: if OCR saw real words but genuinely never saw the
            # target, that's the signature of a region getting blacked out
            # before OCR ever looked at it (e.g. a mis-sized mask rect) rather
            # than a fuzzy-matching problem. Print what it actually found --
            # and break it down spatially, because "OCR only ever sees the
            # tab bar and nothing below it" looks identical to "no match" in
            # a flat word list, but is a completely different bug (a capture
            # problem, not a matching problem).
            sh_frame = cv_frame.shape[0]
            band_top = [w for w in all_seen if w[2] < sh_frame * 0.15]
            band_rest = [w for w in all_seen if w[2] >= sh_frame * 0.15]
            print(f"[OCR] No match for '{query}' above threshold "
                  f"(closest: '{best_overall[1]}' @ score {best_overall[0]}, "
                  f"needed {OCR_MATCH_SCORE}; {len(all_seen)} words seen, "
                  f"{time.time()-t0:.2f}s)")
            print(f"[OCR] Spatial breakdown: {len(band_top)} words in top 15% "
                 f"of frame (tab bar/chrome), {len(band_rest)} words in the "
                 f"rest of the page (frame height {sh_frame}px)")
            if band_rest:
                sample = ", ".join(f"'{w}'@({x},{y})" for w, x, y in band_rest[:20])
                print(f"[OCR] Words from the actual page content: {sample}"
                     + (" ..." if len(band_rest) > 20 else ""))
            else:
                print(f"[OCR] ZERO words detected anywhere below the top 15% "
                     f"of the frame -- OCR is not reading the actual page "
                     f"content at all, only chrome/tabs. This points at the "
                     f"capture itself (wrong region, wrong monitor, or a "
                     f"scaling mismatch cropping out everything below the "
                     f"tab bar), not at text-matching.")
            return None
        except Exception as e:
            print(f"[OCR] Error: {e}")
            return None

    def _try_ocr_nth(self, item_type: str, n: int):
        try:
            if not OCR_AVAILABLE:
                return None
            cv_frame = self._get_frame_for_ocr()
            if cv_frame is None:
                return None
            sw, sh = pyautogui.size()
            gray = cv2.cvtColor(cv_frame, cv2.COLOR_BGR2GRAY)
            rect = self._sane_mask_rect(self._get_mistai_window_rect(), gray.shape)
            if rect:
                l, t, r, b = rect
                h, w = gray.shape
                l, t, r, b = max(l, 0), max(t, 0), min(r, w), min(b, h)
                gray[t:b, l:r] = 0
            data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT,
                                             config="--oem 3 --psm 11")

            candidates = []
            for i, word in enumerate(data["text"]):
                if not word.strip():
                    continue
                try:
                    conf = int(data["conf"][i])
                except (ValueError, TypeError):
                    continue
                if conf < OCR_MIN_CONF:
                    continue
                x = data["left"][i] + data["width"][i] // 2
                y = data["top"][i] + data["height"][i] // 2
                candidates.append((x, y))

            candidates.sort(key=lambda c: (c[1], c[0]))
            if len(candidates) >= n:
                x, y = candidates[n - 1]
                if CoordinateValidator.is_valid(x, y, sw, sh):
                    print(f"[OCR] Found #{n} item @ ({x}, {y})")
                    return (x, y)
            return None
        except Exception as e:
            print(f"[OCR] Nth error: {e}")
            return None

    def _execute_click(self, coords, wait_before: float = 0.15) -> bool:
        try:
            x, y = coords
            self.last_coords = coords
            time.sleep(wait_before)
            sw, sh = pyautogui.size()

            if y < 5 or y > sh - 5 or x < 5 or x > sw - 5:
                return False

            if self.last_method == "Vision" and y < 80:
                y = min(y + 30, sh - 20)

            self._show_debug_marker(x, y)

            pyautogui.moveTo(x, y, duration=0.2)
            time.sleep(0.1)
            pyautogui.click()
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def _show_debug_marker(self, x, y):
        """Flash a color-coded dot at the exact pixel about to be clicked --
        green=OCR, blue=UIA, orange=Vision. Lets you SEE a mis-resolved
        coordinate (a Vision guess landing next to the real button, say)
        instead of having to infer it from logs after the fact.

        Deliberately NOT a silent try/except-pass: a debug tool that hides
        its own failures is worse than no debug tool. If this can't run,
        say so once, loudly, in the console -- so 'nothing showed up' is
        diagnosable instead of a mystery."""
        try:
            from .click_indicator import get_indicator
            get_indicator().show(x, y, method=self.last_method or "direct",
                                 label=self.last_query)
            print(f"[ClickIndicator] Marker requested at ({x}, {y}) "
                  f"method={self.last_method}")
        except Exception as e:
            print(f"[ClickIndicator] Could not show debug marker: {e}")