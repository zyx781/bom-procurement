from __future__ import annotations

"""淘宝滑块自动求解（ddddocr 缺口识别 + OpenCV 兜底 + 拟人拖动）。

支持两种形态，按页面实际元素自动选：
  1. puzzle   —— 拼图滑块（geetest 风格）：canvas 背景 + 缺口 + 拖动按钮，
                 用 ddddocr.slide_match 找缺口 x，按缩放换算成屏幕像素再拖。
  2. drag_end —— 阿里 nc 滑块（"按住滑块，拖动到最右边"）：没有缺口图，
                 直接量轨道宽度拖到底。

设计约束（都很重要）：
  - 重依赖（ddddocr / cv2 / numpy）**延迟导入**：没装包时本模块照样能 import，
    主流程降级成「刷新 + 人工」，不会因为缺包把整个会话打崩。
  - 一切异常都吞掉并返回 False：求解失败只是回到原来的自恢复路径，
    绝不能因为解法出错而让调用方拿到未定义状态。
  - 全程有总时限（默认 45s）：滑块解不开时不要在这里挂死，宁早交给人工。
"""

import os
import random
import sys
import time
from contextlib import contextmanager, suppress
from typing import Any

from taobao_selectors import (
    CAPTCHA_BG_SELECTORS,
    CAPTCHA_PANEL_SELECTORS,
    CAPTCHA_REFRESH_SELECTORS,
    CAPTCHA_SLICE_SELECTORS,
    CAPTCHA_SLIDER_BTN_SELECTORS,
    CAPTCHA_SUCCESS_SELECTORS,
)

_cv2: Any = None
_np: Any = None


def _load_cv() -> bool:
    """按需导入 opencv / numpy。"""
    global _cv2, _np
    if _cv2 is not None and _np is not None:
        return True
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        _cv2, _np = cv2, np
        return True
    except Exception:
        return False


def _load_ddddocr():
    try:
        import ddddocr  # noqa: PLC0415

        return ddddocr
    except Exception:
        return None


def pick_mode(has_bg: bool, has_slider: bool, text: str = "") -> str | None:
    """纯函数：该用哪种解法。便于单测，不碰浏览器。

    - 有背景图 + 有滑块按钮 → puzzle（找缺口）
    - 只有按钮（阿里 nc 那种"拖到最右"）→ drag_end
    - 按钮都没有 → None（不是滑块，别硬拖）
    """
    if not has_slider:
        return None
    if has_bg:
        return "puzzle"
    if any(k in (text or "") for k in ("拖到最右", "拖动到最右", "拖至最右", "拖动滑块")):
        return "drag_end"
    return "drag_end"


def drag_distance_for(track_x: float, track_w: float, btn_x: float, btn_w: float) -> int:
    """纯函数：nc 滑块要拖多远（按钮中心 → 轨道右端再留 2px）。

    阿里那套判定是「按钮必须贴到轨道末端」，少拖几像素就判失败，
    所以这里算到右边界、只留 2px 余量（贴边但不越界）。
    """
    end = track_x + track_w - 2
    start = btn_x + btn_w / 2
    return max(0, int(round(end - start)))


class SliderSolver:
    """Automatic slider CAPTCHA solver using ddddocr (ML) + OpenCV (fallback)."""

    # 阿里滑块"拖到最右"没有缺口图，轨道宽度量不到时的经验值（px）
    DEFAULT_TRACK_PX = 300
    # 淘宝的 nc 轨道就 260~340px；超过这个数一定量到了外层容器
    MAX_TRACK_PX = 520
    TRACK_SELECTORS = (
        ".nc-container", ".nc_scale", "#nc_1_wrapper", ".nc_scale_text",
        "[class*='nc_scale']", "[class*='nc-container']",
    )

    def __init__(self, method: str = "auto") -> None:
        self.method = method
        self._det: Any = None
        self._solves = 0
        ddddocr = _load_ddddocr() if method in ("auto", "ddddocr") else None
        if ddddocr is not None:
            try:
                with suppress(TypeError):
                    self._det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
                if self._det is None:
                    self._det = ddddocr.DdddOcr(det=False, ocr=False)
            except Exception as exc:  # noqa: BLE001
                print(f"[slider] ddddocr init failed: {exc}", file=sys.stderr)
                self._det = None
        if self._det is None and method == "ddddocr":
            raise RuntimeError("ddddocr 不可用")

    # ──────────────────────────────────────────────
    # 可用性
    # ──────────────────────────────────────────────

    @staticmethod
    def available() -> tuple[bool, str]:
        """返回 (能否自动解, 原因)。调用方据此决定是否尝试。"""
        if _load_ddddocr() is not None:
            return True, "ddddocr"
        if _load_cv():
            return True, "opencv"
        return False, "没装 ddddocr/opencv（pip install -r requirements-optional.txt）"

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def is_captcha_present(self, page: Any) -> bool:
        """页面上还有没有滑块（快检，不做长等待）。"""
        return self._probe(page) is not None

    def detect_mode(self, page: Any) -> str | None:
        hit = self._probe(page)
        if hit is None:
            return None
        return pick_mode(hit.get("bg", False), hit.get("btn", False), hit.get("text", ""))

    def solve(self, page: Any, max_retries: int | None = None,
              deadline_s: float | None = None) -> bool:
        """尝试过掉当前滑块。True = 已解除。"""
        tries = max_retries if max_retries is not None else int(
            os.environ.get("TAOBAO_SLIDER_TRIES", "2")
        )
        budget = deadline_s if deadline_s is not None else float(
            os.environ.get("TAOBAO_SLIDER_SECONDS", "45")
        )
        deadline = time.monotonic() + max(5.0, budget)
        # Playwright 的 bounding_box()/evaluate() 不接受 timeout，会用页面默认
        # 30s；一个卡住的调用就能拖垮整次求解（实测启动阶段卡了 3 分钟）。
        # 求解期间把默认超时收紧到 2s，结束一定恢复 —— 这一步是硬保障，
        # 光靠循环里的 deadline 检查救不了「卡在单个调用里」的情况。
        op_ms = int(float(os.environ.get("TAOBAO_SLIDER_OP_MS", "2000")))
        with self._tight_timeout(page, op_ms):
            return self._solve_loop(page, tries, deadline)

    @staticmethod
    @contextmanager
    def _tight_timeout(page: Any, op_ms: int):
        """临时收紧 page 默认超时；假 Page / 不支持时安静跳过。"""
        old: Any = None
        changed = False
        try:
            old = page.context.default_timeout
        except Exception:
            pass
        try:
            page.set_default_timeout(op_ms)
            changed = True
        except Exception:
            changed = False
        try:
            yield
        finally:
            if changed:
                with suppress(Exception):
                    page.set_default_timeout(old if old else 30000)

    def _solve_loop(self, page: Any, tries: int, deadline: float) -> bool:
        for attempt in range(max(1, tries)):
            if time.monotonic() > deadline:
                print("[slider] 超时，交回上层处理", file=sys.stderr)
                return False
            mode = self.detect_mode(page)
            if mode is None:
                # 已经没有滑块了（上一步就解掉 / 页面自己恢复）
                return not self.is_captcha_present(page)
            self._solves += 1
            print(f"[slider] 第 {attempt + 1}/{tries} 次，模式={mode}", file=sys.stderr)
            try:
                ok = self._solve_puzzle(page, deadline) if mode == "puzzle" \
                    else self._solve_drag_end(page, deadline)
            except Exception as exc:  # noqa: BLE001
                print(f"[slider] 解法异常: {exc}", file=sys.stderr)
                ok = False
            if ok and not self.is_captcha_present(page):
                print("[slider] 已通过", file=sys.stderr)
                return True
            if attempt + 1 < tries:
                self._refresh_captcha(page)
                time.sleep(random.uniform(0.6, 1.2))
        print("[slider] 重试用完，仍未通过", file=sys.stderr)
        return False

    # ──────────────────────────────────────────────
    # 页面探测
    # ──────────────────────────────────────────────

    _PROBE_JS = """
    (sels) => {
      const vis = (s) => {
        try {
          return Array.from(document.querySelectorAll(s)).some((el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 4 && r.height > 4 && st.visibility !== 'hidden'
              && st.display !== 'none' && Number(st.opacity) > 0.05;
          });
        } catch (e) { return false; }
      };
      return {
        bg: sels.bg.some(vis),
        btn: sels.btn.some(vis),
        panel: sels.panel.some(vis),
        text: (document.body ? document.body.innerText : '').slice(0, 3000),
      };
    }
    """

    def _scopes(self, page: Any) -> list[Any]:
        """主文档 + 看起来是验证层的 iframe（淘宝 punish/baxia 弹层在 iframe 里）。"""
        scopes = [page]
        with suppress(Exception):
            for frame in page.frames:
                try:
                    if frame == page.main_frame:
                        continue
                except Exception:
                    pass
                fu = (getattr(frame, "url", "") or "").lower()
                if any(k in fu for k in ("punish", "baxia", "captcha", "x5secdata", "_____tmd_____")):
                    scopes.append(frame)
        return scopes

    def _probe(self, page: Any) -> dict | None:
        sels = {
            "bg": list(CAPTCHA_BG_SELECTORS),
            "btn": list(CAPTCHA_SLIDER_BTN_SELECTORS),
            "panel": list(CAPTCHA_PANEL_SELECTORS),
        }
        best = None
        for scope in self._scopes(page):
            try:
                info = scope.evaluate(self._PROBE_JS, sels)
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            if info.get("btn") or info.get("bg"):
                if info.get("btn"):
                    return info
                best = best or info
        return best

    def _find_scope_with(self, page: Any, selectors: list[str]) -> tuple[Any, Any, str] | None:
        """找到第一个能看见目标元素的 (scope, locator, selector)。"""
        for scope in self._scopes(page):
            for sel in selectors:
                with suppress(Exception):
                    loc = scope.locator(sel).first
                    if loc.is_visible(timeout=400):
                        return scope, loc, sel
        return None

    # ──────────────────────────────────────────────
    # 两种解法
    # ──────────────────────────────────────────────

    def _solve_puzzle(self, page: Any, deadline: float) -> bool:
        found_bg = self._find_scope_with(page, CAPTCHA_BG_SELECTORS)
        found_btn = self._find_scope_with(page, CAPTCHA_SLIDER_BTN_SELECTORS)
        if found_btn is None:
            print("[slider/puzzle] 找不到拖动按钮", file=sys.stderr)
            return False
        scope, btn, _ = found_btn
        bg = found_bg[1] if found_bg else None

        if bg is None:
            # 没有背景 canvas 的（阿里 nc）按拖到最右处理
            return self._solve_drag_end(page, deadline)

        bg_bytes = self._shot(bg)
        if not bg_bytes:
            print("[slider/puzzle] 背景截图失败", file=sys.stderr)
            return False
        slice_bytes = None
        found_slice = self._find_scope_with(page, CAPTCHA_SLICE_SELECTORS)
        if found_slice:
            slice_bytes = self._shot(found_slice[1])

        gap_x = self._detect_gap(bg_bytes, slice_bytes)
        if not gap_x or gap_x <= 0:
            print("[slider/puzzle] 缺口识别失败", file=sys.stderr)
            return False

        distance = gap_x
        with suppress(Exception):
            bg_box = bg.bounding_box()
            if bg_box:
                bg_img = self._decode(bg_bytes)
                if bg_img is not None:
                    # 截图是设备像素、元素是 CSS 像素：按背景图宽度换算
                    distance = gap_x * (bg_box["width"] / bg_img.shape[1])
        distance = max(10, int(distance))
        print(f"[slider/puzzle] 缺口 x={gap_x}，拖动 {distance}px", file=sys.stderr)
        self._human_drag(page, btn, distance)
        return self._wait_gone(page, deadline)

    def _solve_drag_end(self, page: Any, deadline: float) -> bool:
        found = self._find_scope_with(page, CAPTCHA_SLIDER_BTN_SELECTORS)
        if found is None:
            print("[slider/drag] 找不到拖动按钮", file=sys.stderr)
            return False
        scope, btn, sel = found
        box = btn.bounding_box()
        if box is None:
            return False
        track = self._track_box(scope, sel, box)
        if track:
            distance = drag_distance_for(track["x"], track["width"], box["x"], box["width"])
        else:
            distance = int(self.DEFAULT_TRACK_PX - box["width"])
        # 实测坑：外层容器 800px 被当成轨道 → 拖了 729px，滑块直接飞出去。
        # 淘宝轨道不会超过 340，量大了就按经验值走。
        if distance > self.MAX_TRACK_PX:
            print(f"[slider/drag] 轨道量出 {distance}px 不合理，回退 {self.DEFAULT_TRACK_PX}px",
                  file=sys.stderr)
            distance = int(self.DEFAULT_TRACK_PX - box["width"])
        distance = max(40, distance)
        print(f"[slider/drag] 拖动 {distance}px 到轨道末端", file=sys.stderr)
        self._human_drag(page, btn, distance)
        return self._wait_gone(page, deadline)

    def _track_box(self, scope: Any, btn_sel: str, btn_box: dict) -> dict | None:
        """量轨道：先按已知选择器，再往上找宽度合理的祖先。

        只认「宽度在按钮 1.5~6 倍之间」的容器 —— 以前只判 >1.5 倍，
        结果整页 800px 的 wrapper 也算轨道，拖出去 729px 必然过不了。
        """
        for sel in self.TRACK_SELECTORS:
            with suppress(Exception):
                got = scope.locator(sel).first.bounding_box()
                if got and btn_box["width"] * 1.5 <= got["width"] <= self.MAX_TRACK_PX:
                    return got
        js = """
        (sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          const b = el.getBoundingClientRect();
          let p = el.parentElement;
          for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
            const r = p.getBoundingClientRect();
            if (r.width > b.width * 1.5) return {x: r.x, y: r.y, width: r.width, height: r.height};
          }
          return null;
        }
        """
        with suppress(Exception):
            got = scope.evaluate(js, btn_sel)
            if isinstance(got, dict) and btn_box["width"] * 1.5 <= got.get("width", 0) <= self.MAX_TRACK_PX:
                return got
        return None

    # ──────────────────────────────────────────────
    # 缺口识别
    # ──────────────────────────────────────────────

    def _decode(self, img_bytes: bytes):
        if not _load_cv():
            return None
        with suppress(Exception):
            return _cv2.imdecode(_np.frombuffer(img_bytes, _np.uint8), _cv2.IMREAD_UNCHANGED)
        return None

    def _detect_gap(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        """找缺口左边缘。三条路依次试，谁先给出可信答案用谁：

        1. ddddocr 模板匹配 —— 真实拼图最准；
        2. 轮廓检测 —— 切片画布和背景重叠时会「自己匹配自己」（结果恒为 0），
           这时候只有按缺口边框找才靠谱；
        3. Sobel 列能量 —— 兜底，精度差但总比没有强。
        """
        got = self._detect_gap_ddddocr(bg_bytes, slice_bytes)
        if got is not None and got > 4:
            return got
        got = self._detect_gap_contour(bg_bytes)
        if got is not None:
            return got
        return self._detect_gap_opencv(bg_bytes, slice_bytes)

    def _detect_gap_contour(self, bg_bytes: bytes) -> int | None:
        """按「近方形、有清晰边框、面积够大」的轮廓找缺口，返回左边缘。"""
        if not _load_cv():
            return None
        try:
            bg = _cv2.imdecode(_np.frombuffer(bg_bytes, _np.uint8), _cv2.IMREAD_COLOR)
            if bg is None:
                return None
            gray = _cv2.cvtColor(bg, _cv2.COLOR_BGR2GRAY)
            edges = _cv2.dilate(
                _cv2.Canny(_cv2.GaussianBlur(gray, (5, 5), 0), 50, 150),
                _np.ones((3, 3), _np.uint8), iterations=1,
            )
            contours, _ = _cv2.findContours(edges, _cv2.RETR_EXTERNAL, _cv2.CHAIN_APPROX_SIMPLE)
            h, w = gray.shape
            best = None
            for cnt in contours:
                x, y, cw, ch = _cv2.boundingRect(cnt)
                if not (w * 0.04 <= cw <= w * 0.45 and h * 0.08 <= ch <= h * 0.75):
                    continue
                if abs(cw - ch) > max(cw, ch) * 0.55:
                    continue
                if x < w * 0.06 or x > w * 0.9:
                    continue
                area = _cv2.contourArea(cnt)
                if area < cw * ch * 0.25:
                    continue
                # 越靠右越可能是缺口（滑块起点在左），面积大的优先
                score = area * (1 + x / max(1, w))
                if best is None or score > best[0]:
                    best = (score, x, cw, ch)
            if best is None:
                return None
            print(f"[slider/contour] 缺口 x={best[1]} 尺寸 {best[2]}x{best[3]}", file=sys.stderr)
            return int(best[1])
        except Exception as exc:  # noqa: BLE001
            print(f"[slider/contour] 异常: {exc}", file=sys.stderr)
            return None

    def _detect_gap_ddddocr(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        """ddddocr 返回的是**模板中心**（1.6 起 `center_x = max_loc + w//2`），
        拖动距离要的是左边缘，所以必须减掉半个模板宽 —— 不少教程直接拿
        target[0] 去拖，结果稳定偏半个拼图，怎么刷都过不了。"""
        if not slice_bytes:
            return None
        try:
            result = self._det.slide_match(slice_bytes, bg_bytes, simple_target=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[slider/ddddocr] 识别失败: {exc}", file=sys.stderr)
            return None
        if not result or not result.get("target"):
            return None
        bg_img = self._decode(bg_bytes)
        sl_img = self._decode(slice_bytes)
        if bg_img is None or sl_img is None:
            return None
        bg_w = bg_img.shape[1]
        sl_w = sl_img.shape[1]
        if sl_w >= bg_w * 0.6:
            # 切片画布是整幅尺寸（geetest 有时这么画）：中心值没法反推左边缘，
            # 交给 opencv 按缺口边缘找，别硬算。
            print("[slider/ddddocr] 切片是整幅画布，改用边缘检测", file=sys.stderr)
            return None
        left = int(result["target"][0]) - sl_w // 2
        print(f"[slider/ddddocr] 缺口左边缘 x={left}（中心 {result['target'][0]}）", file=sys.stderr)
        if 0 <= left <= bg_w:
            return left
        return None

    def _detect_gap_opencv(self, bg_bytes: bytes, slice_bytes: bytes | None = None) -> int | None:
        if not _load_cv():
            return None
        try:
            bg = _cv2.imdecode(_np.frombuffer(bg_bytes, _np.uint8), _cv2.IMREAD_COLOR)
            if bg is None:
                return None
            bg_gray = _cv2.cvtColor(bg, _cv2.COLOR_BGR2GRAY)
            bg_edge = _cv2.Canny(_cv2.GaussianBlur(bg_gray, (5, 5), 0), 100, 200)

            if slice_bytes:
                slider = _cv2.imdecode(_np.frombuffer(slice_bytes, _np.uint8), _cv2.IMREAD_UNCHANGED)
                if slider is not None and slider.shape[1] < bg.shape[1] * 0.6:
                    if len(slider.shape) == 3 and slider.shape[2] == 4:
                        slider = slider[:, :, :3]
                    slider_gray = _cv2.cvtColor(slider, _cv2.COLOR_BGR2GRAY)
                    slider_edge = _cv2.Canny(_cv2.GaussianBlur(slider_gray, (5, 5), 0), 100, 200)
                    result = _cv2.matchTemplate(bg_edge, slider_edge, _cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = _cv2.minMaxLoc(result)
                    print(f"[slider/opencv] 缺口 x={max_loc[0]} 置信 {max_val:.3f}", file=sys.stderr)
                    return int(max_loc[0])

            sobel_x = _cv2.Sobel(bg_gray, _cv2.CV_64F, 1, 0, ksize=3)
            col_sums = _np.sum(_np.abs(sobel_x).astype(_np.uint8), axis=0)
            min_x = int(bg.shape[1] * 0.2)
            max_x = int(bg.shape[1] * 0.9)
            if max_x <= min_x:
                return None
            gap_x = int(_np.argmax(col_sums[min_x:max_x])) + min_x
            print(f"[slider/opencv] 估算缺口 x={gap_x}", file=sys.stderr)
            return gap_x
        except Exception as exc:  # noqa: BLE001
            print(f"[slider/opencv] 异常: {exc}", file=sys.stderr)
            return None

    # ──────────────────────────────────────────────
    # 拟人拖动
    # ──────────────────────────────────────────────

    def _shot(self, locator: Any) -> bytes | None:
        with suppress(Exception):
            return locator.screenshot()
        return None

    def _human_drag(self, page: Any, slider_btn: Any, distance: int) -> None:
        box = slider_btn.bounding_box()
        if box is None:
            return
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        trajectory = self._generate_trajectory((start_x, start_y), (start_x + distance, start_y))

        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.08, 0.22))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.12))
        for x, y, delay_ms in trajectory:
            page.mouse.move(x, y)
            time.sleep(delay_ms / 1000.0)
        time.sleep(random.uniform(0.06, 0.18))
        page.mouse.up()

    def _generate_trajectory(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> list[tuple[float, float, int]]:
        """贝塞尔轨迹 + 手抖 + 变速 + 六成概率过冲回拉。"""
        steps = random.randint(28, 52)
        cp1 = (
            start[0] + (end[0] - start[0]) * random.uniform(0.15, 0.35),
            start[1] + random.uniform(-14, 14),
        )
        cp2 = (
            start[0] + (end[0] - start[0]) * random.uniform(0.65, 0.85),
            start[1] + random.uniform(-10, 10),
        )

        points: list[tuple[float, float, int]] = []
        for i in range(steps + 1):
            t = i / steps
            x = (
                (1 - t) ** 3 * start[0]
                + 3 * (1 - t) ** 2 * t * cp1[0]
                + 3 * (1 - t) * t ** 2 * cp2[0]
                + t ** 3 * end[0]
            )
            y = (
                (1 - t) ** 3 * start[1]
                + 3 * (1 - t) ** 2 * t * cp1[1]
                + 3 * (1 - t) * t ** 2 * cp2[1]
                + t ** 3 * end[1]
            )
            x += random.gauss(0, 0.6)
            y += random.gauss(0, 0.6)
            if i == 0:
                delay = random.randint(30, 70)
            elif t < 0.3:
                delay = random.randint(8, 18)
            elif t < 0.7:
                delay = random.randint(3, 9)
            elif t < 0.95:
                delay = random.randint(8, 22)
            else:
                delay = random.randint(18, 50)
            points.append((x, y, delay))

        if random.random() < 0.6:
            points.insert(-1, (end[0] + random.uniform(2, 7), end[1] + random.gauss(0, 1),
                               random.randint(12, 35)))
            points.append((end[0] + random.gauss(0, 0.4), end[1] + random.gauss(0, 0.4),
                           random.randint(10, 25)))
        return points

    # ──────────────────────────────────────────────
    # 收尾
    # ──────────────────────────────────────────────

    def _wait_gone(self, page: Any, deadline: float) -> bool:
        """拖完给页面一点时间出结果，最多等到总时限。"""
        limit = min(deadline, time.monotonic() + 6.0)
        while time.monotonic() < limit:
            time.sleep(0.5)
            if not self.is_captcha_present(page):
                return True
            for sel in CAPTCHA_SUCCESS_SELECTORS:
                with suppress(Exception):
                    if page.locator(sel).first.is_visible(timeout=200):
                        time.sleep(0.5)
                        return True
        return not self.is_captcha_present(page)

    def _check_solved(self, page: Any) -> bool:
        if not self.is_captcha_present(page):
            return True
        for sel in CAPTCHA_SUCCESS_SELECTORS:
            with suppress(Exception):
                if page.locator(sel).first.is_visible(timeout=300):
                    return True
        for text in ("验证成功", "通过验证"):
            with suppress(Exception):
                if page.locator(f"text={text}").first.is_visible(timeout=300):
                    return True
        return False

    def _refresh_captcha(self, page: Any) -> None:
        for scope in self._scopes(page):
            for sel in CAPTCHA_REFRESH_SELECTORS:
                with suppress(Exception):
                    btn = scope.locator(sel).first
                    if btn.is_visible(timeout=300):
                        btn.click()
                        print("[slider] 已换一张验证图", file=sys.stderr)
                        return

    # 兼容旧调用名（之前只有这两处被内部用到）
    _screenshot_element = _shot
