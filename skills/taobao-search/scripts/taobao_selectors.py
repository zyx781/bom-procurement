"""Centralized CSS / text selectors for Taobao/Tmall DOM interaction.

When Taobao changes their UI, update selectors here — not scattered across adapter files.
"""

# ──────────────────────────────────────────────
# Search & Navigation
# ──────────────────────────────────────────────

SEARCH_INPUT = [
    "#q",
    "input[name='q']",
    "input[placeholder*='搜索']",
    "input[aria-label*='搜索']",
    "input.search-combobox-input",
    "input[class*='search']",
]

SEARCH_SUBMIT = [
    "button[type='submit']",
    "#J_TSearchForm button",
    "button:has-text('搜索')",
    ".btn-search",
]

POPUP_CLOSE_BUTTONS = [
    "button:has-text('关闭')",
    "button:has-text('我知道了')",
    "text=关闭",
    "text=我知道了",
]

MIDDLEWARE_OVERLAY_HIDE_JS = (
    """() => {
        document.querySelectorAll('.J_MIDDLEWARE_FRAME_WIDGET, [class*="middleware"], [class*="overlay"]').forEach(el => {
            el.style.display = 'none';
        });
    }"""
)

# ──────────────────────────────────────────────
# Product Links (search result cards)
# ──────────────────────────────────────────────

PRODUCT_LINK_SELECTORS = [
    "a[href*='item.htm']",
    "a[href*='detail.tmall.com']",
    "a[href*='taobao.com/item.htm']",
    "a[href*='tmall.com/item.htm']",
    "a[href*='item.taobao.com']",
]

PRODUCT_CARD_CLIMB_SELECTORS = [
    "[class*='item']",
    "[class*='Item']",
    "div[class*='card']",
    "[class*='Card']",
    "[class*='grid']",
    "[class*='Grid']",
]

# ──────────────────────────────────────────────
# Login Detection
# ──────────────────────────────────────────────

LOGIN_PAGE_URL_SIGNALS = [
    "login.taobao.com",
    "login.tmall.com",
]

NOT_LOGGED_IN_TEXT = [
    "text=亲，请登录",
    "text=请登录",
]

LOGGED_IN_INDICATORS = [
    "text=退出",
    "text=我的淘宝",
    "text=已登录",
    ".site-nav-user .site-nav-login-info-nick",
    ".site-nav-user .site-nav-icon",
    ".J_UserMember",
    ".tb-header-username",
    "[class*='user-nick']",
    "[class*='userName']",
    ".site-nav-login-info-nick",
]

# 只有 unb 能证明「登录过」：它是登录后写进 .taobao.com 的用户编号。
# cookie2 / _tb_token_ 游客一进首页就有（实测 2026-11-19：全新 profile 打开
# www.taobao.com 后 cookie2、_tb_token_ 都在，unb 没有）。把这两个算成登录
# 证据，点完「登录」页面一跳转就判成「登录成功」——新用户还没扫码就被宣布
# 登录完成，站点随即把浏览器藏起来，人连码都扫不到。
LOGIN_COOKIE_NAMES = {"unb"}

# ──────────────────────────────────────────────
# Access Control / Risk Detection
# ──────────────────────────────────────────────

ACCESS_BLOCKED_SIGNALS = [
    "text=访问被拒绝",
    "text=异常流量",
    # 淘宝自己的滑块弹层：「亲，请拖动下方滑块完成验证」「请按住滑块，拖动到最右边」
    "text=请拖动滑块",
    "text=拖动下方滑块",
    "text=请按住滑块",
    "text=向右拖动滑块",
    "text=滑块验证",
    "text=请完成验证",
]
# 注意：不要写裸的 "text=验证"。
# 商品页上「正品验证 / 验证码 / 已验证」这类正常文案会让整个批量误判成撞验证码并卡死。

# ──────────────────────────────────────────────
# Product Detail Page
# ──────────────────────────────────────────────

PRICE_SELECTORS = [
    "#J_StrPr498",
    ".tm-price",
    ".tb-rmb-num",
    '[class*="price"]:not([class*="price-"])',
    ".tm-promo-price .tm-price",
    ".tb-item-price",
    ".sku-price .price-value",
    ".J_original_price",
]

# 「详情页可以开始干活了」的信号：价格、SKU 区块、加购按钮，任一出现即可。
# 单规格商品没有 SKU 区块，所以不能只等 [class*="sku"] —— 那会白等满超时。
PRICE_READY_SELECTORS = [
    '[class*="price"]',
    '[class*="sku"]',
    "button:has-text('加入购物车')",
    "a:has-text('加入购物车')",
    "text=加入购物车",
]

SALES_COUNT_SELECTORS = [
    '[class*="sale"]',
    '[class*="Sell"]',
    '[class*="deal"]',
    '[class*="count"]:not([class*="comment"]):not([class*="rate"])',
    ".tm-ind-sellCount",
    ".tb-sell-counter",
]

RATING_SELECTORS = [
    '[class*="rate"]',
    '[class*="Rating"]',
    '[class*="rating"]',
    '[class*="score"]',
    '[class*="positive"]',
    '[class*="好评"]',
    ".dsr-item",
    ".tm-ind-item",
    ".tb-seller-rate",
    "#dsr-info",
    '[class*="seller"]',
    '[class*="star"]',
    ".tb-rate",
    ".tm-rate",
    ".J_TitleRate",
    ".tm-ind-title",
]

# Taobao uses CSS Modules with dynamic hash suffixes (e.g. skuWrapper--iKSsnB_s).
# Use prefix matching via [class*="..."] to stay resilient against hash changes.
SKU_CONTAINER_SELECTORS = [
    '[class*="GeneralSkuPanel"]',
    '[class*="skuWrapper"]',
    '[class*="PurchasePanel"]',
    ".J_TSaleProp",
    ".tb-prop",
    ".tb-sku",
    "[data-property]",
    ".tm-sale-prop",
    'dl[class*="prop"]',
    ".J_Prop",
]

SKU_OPTION_SELECTORS = [
    '[class*="skuItem"]:not([class*="disabled"]):not([class*="disable"])',
    ".J_TSaleProp li:not([class*='disabled']):not([class*='out-of'])",
    ".tb-prop li:not([class*='disabled']):not([class*='out-of'])",
    ".tb-sku li:not([class*='disabled'])",
    "dl[class*='prop'] dd a:not([class*='disabled'])",
    "[data-property] li:not([class*='disabled'])",
]

# JS-side selectors for _select_default_sku's page.evaluate()
SKU_GROUP_JS_SELECTORS = [
    '[class*="skuWrapper"]',
    '[class*="GeneralSkuPanel"]',
    ".J_TSaleProp", ".tb-prop", ".tb-sku", "[data-property]",
    ".tm-sale-prop", 'dl[class*="prop"]', ".J_Prop",
]

SKU_ITEM_JS_SELECTORS = [
    '[class*="valueItem"]',
    '[class*="skuItem"]',
    "li", "dd a", 'span[data-value]', 'div[class*="item-sku"]',
]

SKU_VALUE_SELECTOR = '[class*="valueItem"]:not([class*="label"]):not([class*="Label"])'

# ──────────────────────────────────────────────
# Cart
# ──────────────────────────────────────────────

ADD_TO_CART_BUTTONS = [
    "button:has-text('加入购物车')",
    "a:has-text('加入购物车')",
    "text=加入购物车",
    "button:has-text('购物车')",
]

CART_ITEM_SELECTORS = [
    ".cart-list-item",
    ".item-wrapper",
    "[class*='cart-item']",
    ".item-body",
    ".J_ItemBody",
    ".cart-item",
]

# 2024+ cart.taobao.com is an antd/React page. Each cart row is a
# div[class*="cartItemInfoContainer"] holding the title, one
# input.ant-checkbox-input and the row's own 移入收藏 / 删除 links.
# NOTE: [class*="itemWrapper"] is the "猜你喜欢" recommendation card, not a
# cart row — never use it for cart operations.
CART_ITEM_ROW_SELECTORS = [
    '[class*="cartItemInfoContainer"]',
    '[class*="trade-cart-item-info"]',
    '[class*="cartItemInfo"]',
]

CART_ROW_CHECKBOX = 'label.ant-checkbox-wrapper'
CART_SELECT_ALL_CHECKBOX = 'label[class*="cartOperationCheckbox"]'

# Header batch-delete button ("删除") next to 全选 / 移入收藏.
CART_BATCH_DELETE_BUTTONS = [
    'div[class*="cartOperationBtn"]:has-text("删除")',
    '[class*="cartOperationLeft"] div:has-text("删除")',
    'div:text-is("删除")',
]

# Per-row delete link ("删除" on the item itself).
CART_ROW_DELETE_BUTTONS = [
    '[class*="cartOperationItem"]:text-is("删除")',
    '[class*="cartOperationItem"]:has-text("删除")',
    '[class*="trade-cart-item-operation"] div:has-text("删除")',
]

# Ant Design confirm modal that appears after a delete click.
CART_DELETE_CONFIRM_BUTTONS = [
    ".ant-modal-confirm button.ant-btn-primary",
    ".ant-modal button.ant-btn-primary",
    '[role="dialog"] button.ant-btn-primary',
    'button:has-text("确认删除")',
    'button:has-text("确定")',
    'button:has-text("确认")',
]

# ──────────────────────────────────────────────
# Purchase Quantity (detail page)
# ──────────────────────────────────────────────

QUANTITY_INPUT_SELECTORS = [
    # 2024+ detail page: <div class="countWrapper--x"><input class="countValue--y" value="1">
    "input[class*='countValue']",
    "[class*='countValueWrapper'] input",
    "[class*='countWrapper'] input",
    "#J_Quantity",
    "input[name='quantity']",
    "[class*='qty'] input[type='text']",
    "[class*='quantity'] input[type='text']",
    "[class*='Quantity'] input[type='text']",
    "[class*='qty'] input",
    "[class*='quantity'] input",
]

QUANTITY_PLUS_SELECTORS = [
    "[class*='quantityBtn'][class*='addBtn']",
    "[class*='addBtn']",
    "[class*='qty'] [class*='plus']",
    "[class*='qty'] [class*='add']",
    "[class*='quantity'] [class*='plus']",
    "[class*='quantity'] [class*='add']",
    "[class*='qty'] a:has-text('+')",
    "[class*='quantity'] a:has-text('+')",
]

QUANTITY_CONTAINER_SELECTORS = [
    "[class*='countWrapper']",
    "[class*='countValue']",
    "[class*='qtyNum']",
    "[class*='quantityNum']",
    "[class*='qty']",
    "[class*='quantity']",
    "[class*='Quantity']",
]

# ──────────────────────────────────────────────
# CAPTCHA / Slider Verification (GeeTest v3/v4)
# ──────────────────────────────────────────────

CAPTCHA_PANEL_SELECTORS = [
    ".geetest_panel_box",
    ".geetest_widget",
    ".geetest_panel",
]

CAPTCHA_BG_SELECTORS = [
    "canvas.geetest_canvas_bg.geetest_absolute",
    ".geetest_canvas_bg",
    ".geetest_bg",
    "canvas[class*='geetest_canvas_bg']",
    ".geetest_panel_bg",
    ".geetest_canvas_bg canvas",
]

CAPTCHA_SLICE_SELECTORS = [
    "canvas.geetest_canvas_slice.geetest_absolute",
    ".geetest_canvas_slice",
    ".geetest_slice",
    "canvas[class*='geetest_canvas_slice']",
    ".geetest_slide_slice",
]

CAPTCHA_SLIDER_BTN_SELECTORS = [
    ".geetest_btn",
    ".geetest_slider_button",
    "div.geetest_btn",
    ".geetest_slider .geetest_slider_button",
    "div[class*='geetest_btn']",
    ".geetest_holder .geetest_slider_button",
    # 淘宝自带的 baxia 滑块（不是 geetest）：滑块按钮 + 弹层容器
    "#nc_1_n1z",
    ".nc_iconfont.btn_slide",
    "[id*='nc_1_n1z']",
    "[class*='nc-container']",
    "[class*='baxia-dialog']",
]

CAPTCHA_SUCCESS_SELECTORS = [
    ".geetest_success",
    ".geetest_result_tip",
    ".geetest_panel_success",
    ".geetest_result_tip.geetest_success",
]

CAPTCHA_REFRESH_SELECTORS = [
    ".geetest_refresh",
    ".geetest_reset_tip_content",
    "button:has-text('刷新')",
    ".captcha-refresh",
    "[class*='refresh']",
]

# Text-only fallbacks — only used if class-based detection misses
CAPTCHA_TEXT_FALLBACKS = [
    "text=请完成验证",
    "text=向右拖动滑块",
    "text=拖动滑块",
    "text=请拖动滑块",
    "text=请先完成验证",
]

# ──────────────────────────────────────────────
# Visual Agent: SKU Group Labels & Structure
# ──────────────────────────────────────────────

SKU_GROUP_LABEL_SELECTORS = [
    "dt[class*='prop']",
    "[class*='sku'] dt",
    ".tb-prop dt",
    "dl[class*='prop'] dt",
    ".tm-prop-label",
    "[class*='skuLabel']",
    "[class*='propTitle']",
    "[class*='skuTitle']",
]

SKU_GROUP_VALUE_CONTAINER = [
    "dd[class*='prop']",
    "dd.tb-prop",
    "dl[class*='prop'] dd",
    "[class*='skuGroup'] [class*='value']",
]

# ──────────────────────────────────────────────
# Visual Agent: Cart Confirmation & Detail Page
# ──────────────────────────────────────────────

CART_CONFIRM_POPUP = [
    ".J_ItemCart",
    ".cart-popup",
    "[class*='cartSuccess']",
    "text=已成功加入购物车",
    ".tm-cart-popup",
    ".tb-cart-popup",
    "[class*='addToCartSuccess']",
]

PRODUCT_DETAIL_IMAGE_SELECTORS = [
    "#J_ImgBod",
    ".tb-booth img",
    ".tb-pic img",
    "[class*='preview'] img",
    "#J_UlThumb img",
]

SORT_OPTION_SELECTORS = [
    ".sort-bar a",
    "[class*='sort'] a",
    ".J_Sort a",
    ".sort-row a",
]
