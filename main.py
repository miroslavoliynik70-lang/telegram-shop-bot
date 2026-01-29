import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from config import BOT_TOKEN, ADMIN_IDS, CURRENCY
import db
from texts import TEXT
import os
from aiohttp import web

def currency_symbol(code: str) -> str:
    code = (code or "").upper()
    if code == "EUR":
        return "€"
    if code == "USD":
        return "$"
    if code == "RUB":
        return "₽"
    return code

def money(amount: int | float) -> str:
    sym = currency_symbol(CURRENCY)
    return f"{amount} {sym}"

async def health_server():
    app = web.Application()

    async def handle(request):
        return web.Response(text="OK")

    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Health server running on port {port}")


def money(cents: int) -> str:
    euros = cents / 100
    return f"{euros:.2f} {CURRENCY}"


class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    pay = State()


class AddWizard(StatesGroup):
    category = State()
    title = State()
    price = State()
    stock = State()
    photo = State()


class ProductEdit(StatesGroup):
    set_stock = State()
    set_price = State()

dp = Dispatcher()
USER_LANG = {}
WAITING_CHANNEL = set()
LAST_UI_MSG = {}  # user_id -> message_id


def lang(user_id: int) -> str:
    # 1) память в процессе
    if user_id in USER_LANG:
        return USER_LANG[user_id]
    # 2) сохранённый выбор в базе
    saved = db.get_setting(f"lang:{user_id}")
    if saved in ("ru", "de"):
        USER_LANG[user_id] = saved
        return saved
    # 3) по умолчанию
    return "ru"


def kb_lang():
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="lang:ru")
    kb.button(text="🇩🇪 Deutsch", callback_data="lang:de")
    kb.adjust(2)
    return kb.as_markup()


def kb_main(lg: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT["catalog"][lg], callback_data="menu:catalog")
    kb.button(text=TEXT["cart"][lg], callback_data="menu:cart")
    kb.adjust(1)
    kb.button(text=TEXT["admin"][lg], callback_data="menu:admin")
    kb.adjust(1)
    return kb.as_markup()


def kb_back(lg: str, to: str = "menu:root"):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT["back"][lg], callback_data=to)
    return kb.as_markup()


def kb_cancel_to(lg: str, to: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT["cancel"][lg], callback_data=to)
    return kb.as_markup()


async def cleanup_prev_ui(bot: Bot, chat_id: int, user_id: int):
    mid = LAST_UI_MSG.get(user_id)
    if not mid:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=mid)
    except Exception:
        pass


async def send_ui(bot: Bot, chat_id: int, user_id: int, text: str, reply_markup=None, photo=None):
    await cleanup_prev_ui(bot, chat_id, user_id)
    if photo:
        msg = await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=reply_markup)
    else:
        msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    LAST_UI_MSG[user_id] = msg.message_id
    return msg


def cart_total_qty(user_id: int) -> int:
    items = db.cart_items(user_id)
    return sum(i[3] for i in items) if items else 0


# ---------------- START / LANG ----------------
@dp.message(F.text.in_({"/start", "start"}))
async def start(message: Message, bot: Bot):
    await send_ui(
        bot, message.chat.id, message.from_user.id,
        TEXT["choose_lang"]["ru"] + "\n" + TEXT["choose_lang"]["de"],
        kb_lang()
    )


@dp.message(F.text == "/whoami")
async def whoami(message: Message):
    await message.answer(f"Your ID: {message.from_user.id}")


@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(call: CallbackQuery, bot: Bot):
    lg = call.data.split(":")[1]
    USER_LANG[call.from_user.id] = lg
    db.set_setting(f"lang:{call.from_user.id}", lg)
    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["menu"][lg], kb_main(lg))


@dp.callback_query(F.data == "menu:root")
async def menu_root(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["menu"][lg], kb_main(lg))


# ---------------- CATALOG ----------------
@dp.callback_query(F.data == "menu:catalog")
async def menu_catalog(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    cats = db.list_categories()

    if not cats:
        await call.answer()
        await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["empty"][lg], kb_back(lg))
        return

    kb = InlineKeyboardBuilder()
    for c in cats:
        kb.button(text=c, callback_data=f"cat:{c}")
    kb.button(text=TEXT["back"][lg], callback_data="menu:root")
    kb.adjust(1)

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["catalog"][lg] + ":", kb.as_markup())


@dp.callback_query(F.data.startswith("cat:"))
async def cat_open(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    category = call.data.split(":", 1)[1]
    products = db.list_products(category)

    kb = InlineKeyboardBuilder()
    for pid, title, price, stock in products:
        kb.button(text=f"{title} — {money(price)} (x{stock})", callback_data=f"p:{pid}")
    kb.button(text=TEXT["back"][lg], callback_data="menu:catalog")
    kb.adjust(1)

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, f"{category}:", kb.as_markup())


@dp.callback_query(F.data.startswith("p:"))
async def product_open(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    pid = int(call.data.split(":")[1])

    p = db.get_product(pid)
    if not p:
        await call.answer("Not found", show_alert=True)
        return

    _id, category, title, price, stock, photo_file_id = p
    total_qty = cart_total_qty(call.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ 1", callback_data=f"add:{pid}:1")
    kb.button(text="➕ 2", callback_data=f"add:{pid}:2")
    kb.button(text="➕ 5", callback_data=f"add:{pid}:5")
    kb.adjust(3)
    kb.button(text=f"{TEXT['cart'][lg]} ({total_qty})", callback_data="menu:cart")
    kb.button(text=TEXT["back"][lg], callback_data=f"cat:{category}")
    kb.adjust(2)

    caption = f"{title}\n{money(price)}\nStock: {stock}"

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, caption, kb.as_markup(), photo=photo_file_id)


# ---------------- CART: add/remove with stock return ----------------
@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(call: CallbackQuery, bot: Bot):
    try:
        _, pid, qty = call.data.split(":")
        pid = int(pid)
        qty = int(qty)

        added = db.cart_add_reserve(call.from_user.id, pid, qty)
        if added <= 0:
            await call.answer("Нет в наличии / Nicht verfügbar", show_alert=True)
            return

        total_qty = cart_total_qty(call.from_user.id)
        lg = lang(call.from_user.id)
        if lg == "de":
            msg = f"✅ Hinzugefügt: +{added}\n🧺 Im Warenkorb: {total_qty}"
        else:
            msg = f"✅ Добавлено: +{added}\n🧺 В корзине: {total_qty}"

        await call.answer(msg, show_alert=True)
    except Exception:
        await call.answer("Ошибка / Error", show_alert=True)


@dp.callback_query(F.data.startswith("rm1:"))
async def remove_one(call: CallbackQuery, bot: Bot):
    try:
        pid = int(call.data.split(":")[1])
        removed = db.cart_remove_return(call.from_user.id, pid, 1)
        await call.answer(f"-{removed}" if removed else "0", show_alert=False)
        await cart_view(call, bot)
    except Exception:
        await call.answer("Ошибка / Error", show_alert=True)


@dp.callback_query(F.data == "cart:clear")
async def cart_clear(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    db.cart_clear_return(call.from_user.id)
    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["empty"][lg], kb_back(lg))


@dp.callback_query(F.data == "menu:cart")
async def cart_view(call: CallbackQuery, bot: Bot):
    lg = lang(call.from_user.id)
    items = db.cart_items(call.from_user.id)

    if not items:
        await call.answer()
        await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["empty"][lg], kb_back(lg))
        return

    total = 0
    lines = []
    kb = InlineKeyboardBuilder()

    for pid, title, price, qty in items:
        total += price * qty
        lines.append(f"• {title} × {qty} = {money(price * qty)}")
        kb.button(text=f"➖ 1 {title}", callback_data=f"rm1:{pid}")

    text = "\n".join(lines) + f"\n\nTotal: {money(total)}"

    kb.button(text=TEXT["checkout"][lg], callback_data="checkout:start")
    kb.button(text=TEXT["clear_cart"][lg], callback_data="cart:clear")
    kb.button(text=TEXT["continue_shop"][lg], callback_data="menu:catalog")
    kb.adjust(1)

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, text, kb.as_markup())


# ---------------- CHECKOUT ----------------
@dp.callback_query(F.data == "checkout:start")
async def checkout_start(call: CallbackQuery, state: FSMContext, bot: Bot):
    lg = lang(call.from_user.id)
    await state.set_state(Checkout.name)
    await call.answer("✍️")
    await send_ui(bot, call.message.chat.id, call.from_user.id, TEXT["ask_name"][lg], kb_cancel_to(lg, "menu:cart"))


@dp.message(Checkout.name)
async def checkout_name(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(name=message.text.strip())
    lg = lang(message.from_user.id)
    await state.set_state(Checkout.phone)
    await send_ui(bot, message.chat.id, message.from_user.id, TEXT["ask_phone"][lg], kb_cancel_to(lg, "menu:cart"))


@dp.message(Checkout.phone)
async def checkout_phone(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(phone=message.text.strip())
    lg = lang(message.from_user.id)
    await state.set_state(Checkout.address)
    await send_ui(bot, message.chat.id, message.from_user.id, TEXT["ask_address"][lg], kb_cancel_to(lg, "menu:cart"))


@dp.message(Checkout.address)
async def checkout_address(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(address=message.text.strip())
    lg = lang(message.from_user.id)
    await state.set_state(Checkout.pay)

    kb = InlineKeyboardBuilder()
    kb.button(text=TEXT["pay_card"][lg], callback_data="pay:card_soon")
    kb.button(text=TEXT["pay_cash"][lg], callback_data="pay:cash")
    kb.button(text=TEXT["cancel"][lg], callback_data="menu:cart")
    kb.adjust(1)

    await send_ui(bot, message.chat.id, message.from_user.id, TEXT["pay_method"][lg], kb.as_markup())


@dp.callback_query(Checkout.pay, F.data.startswith("pay:"))
async def checkout_pay(call: CallbackQuery, state: FSMContext, bot: Bot):
    lg = lang(call.from_user.id)
    pay_method = call.data.split(":")[1]

    data = await state.get_data()
    name = data.get("name")
    phone = data.get("phone")
    address = data.get("address")

    tg_username = call.from_user.username
    tg_name = " ".join(x for x in [call.from_user.first_name, call.from_user.last_name] if x).strip()

    created = db.create_order(
        call.from_user.id, name, phone, address, pay_method,
        tg_username=tg_username, tg_name=tg_name
    )

    if not created:
        await call.answer("Корзина пуста", show_alert=True)
        return

    order_id, total, items = created

    await call.answer("✅")
    await state.clear()

    await send_ui(
        bot, call.message.chat.id, call.from_user.id,
        TEXT["order_done"][lg] + f"\nOrder #{order_id}\nTotal: {money(total)}",
        kb_main(lg)
    )

    # ---- ADMIN уведомление с кнопками ----
    if ADMIN_IDS and ADMIN_IDS != 0:
        tg_link = f"tg://user?id={call.from_user.id}"
        lines = [
            f"🧾 New order #{order_id}",
            f"Status: NEW",
            f"User ID: {call.from_user.id}",
            f"TG name: {tg_name if tg_name else '(empty)'}",
            f"Username: @{tg_username}" if tg_username else "Username: (none)",
            f"Link: {tg_link}",
            "",
            f"Customer name: {name}",
            f"Phone: {phone}",
            f"Address: {address}",
            f"Pay: {pay_method}",
            ""
        ]
        for pid, title, price, qty in items:
            lines.append(f"- {title} x{qty} = {money(price * qty)}")
        lines.append(f"\nTOTAL: {money(total)}")

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять / Accept", callback_data=f"ord:accept:{order_id}")
        kb.button(text="❌ Отклонить / Decline", callback_data=f"ord:decline:{order_id}")
        kb.adjust(2)

        # кнопка написать (ссылка)
        kb.button(text="💬 Написать клиенту / Message", url=tg_link)
        kb.adjust(1)

        await bot.send_message(ADMIN_IDS, "\n".join(lines), reply_markup=kb.as_markup())


# ---------------- ADMIN: accept/decline order ----------------

@dp.callback_query(F.data.startswith("ord:"))
async def admin_order_action(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    try:
        _, action, oid = call.data.split(":")
        order_id = int(oid)
    except Exception:
        await call.answer("Bad data", show_alert=True)
        return

    order = db.get_order(order_id)
    if not order:
        await call.answer("Order not found", show_alert=True)
        return

    _id, user_id, status, tg_username, tg_name, cname, phone, address = order

    if status in ("accepted", "declined"):
        await call.answer("Уже обработано / Already processed", show_alert=True)
        return

    if action == "accept":
        db.set_order_status(order_id, "accepted")
        try:
            await bot.send_message(
                user_id,
                "✅ Ваш заказ подтверждён! Мы скоро свяжемся с вами."
            )
        except Exception:
            pass

        await call.answer("✅ Принято", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "decline":
        db.set_order_status(order_id, "declined")
        db.restock_order(order_id)

        try:
            await bot.send_message(
                user_id,
                "❌ К сожалению, заказ отклонён. Напишите нам, чтобы уточнить детали."
            )
        except Exception:
            pass

        await call.answer("❌ Отклонено", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return


# ---------------- ADMIN: menu + wizard + stock ----------------
@dp.callback_query(F.data == "menu:admin")
async def admin_entry(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return  
   
    kb = InlineKeyboardBuilder()

    # Заказы
    kb.button(text="🧾 Заказы (NEW)", callback_data="admin:orders:new")
    kb.button(text="✅ Заказы (Accepted)", callback_data="admin:orders:accepted")
    kb.button(text="❌ Заказы (Declined)", callback_data="admin:orders:declined")
    kb.button(text="🗑 Заказы (Cancelled)", callback_data="admin:orders:cancelled")
    kb.adjust(2, 2)

    # Товары / склад
    kb.button(text="📦 Товары / Склад", callback_data="admin:products")
    kb.button(text="➕ Добавить товар", callback_data="admin:wizard")
    kb.button(text="📊 Список остатков", callback_data="admin:stock")
    kb.adjust(1)

    # Канал
    kb.button(text="🔗 Привязать канал (/setchannel)", callback_data="admin:setchannel")
    kb.button(text="📢 Пост в канал (/post)", callback_data="admin:post")
    kb.adjust(1)

    kb.button(text="⬅️ Назад", callback_data="menu:root")
    kb.adjust(1)

    await call.answer()
    await bot.send_message(call.message.chat.id, "⚙️ Админка:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "admin:wizard")
async def admin_wizard_start(call: CallbackQuery, state: FSMContext, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        await state.clear()
        return

    await state.set_state(AddWizard.category)
    await call.answer()
    await send_ui(
        bot, call.message.chat.id, call.from_user.id,
        "Категория? (например: Hats / T-shirts)",
        kb_cancel_to(lang(call.from_user.id), "menu:admin")
    )


@dp.message(AddWizard.category)
async def admin_wizard_category(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(category=message.text.strip())
    await state.set_state(AddWizard.title)
    await send_ui(bot, message.chat.id, message.from_user.id, "Название товара?", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.title)
async def admin_wizard_title(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddWizard.price)
    await send_ui(bot, message.chat.id, message.from_user.id, "Цена (например 19.99):", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.price)
async def admin_wizard_price(message: Message, state: FSMContext, bot: Bot):
    try:
        price_cents = int(round(float(message.text.strip().replace(",", ".")) * 100))
    except Exception:
        await send_ui(bot, message.chat.id, message.from_user.id, "Не понял цену. Напиши например 19.99", kb_cancel_to(lang(message.from_user.id), "menu:admin"))
        return
    await state.update_data(price_cents=price_cents)
    await state.set_state(AddWizard.stock)
    await send_ui(bot, message.chat.id, message.from_user.id, "Остаток (число):", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.stock)
async def admin_wizard_stock(message: Message, state: FSMContext, bot: Bot):
    try:
        stock = int(message.text.strip())
    except Exception:
        await send_ui(bot, message.chat.id, message.from_user.id, "Не понял. Напиши число (например 50).", kb_cancel_to(lang(message.from_user.id), "menu:admin"))
        return
    await state.update_data(stock=stock)
    await state.set_state(AddWizard.photo)
    await send_ui(bot, message.chat.id, message.from_user.id, "Теперь пришли ФОТО товара (или напиши 'skip'):", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.photo, F.text.lower() == "skip")
async def admin_wizard_skip_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    db.add_product(data["category"], data["title"], data["price_cents"], data["stock"], photo_file_id=None)
    await state.clear()
    await send_ui(bot, message.chat.id, message.from_user.id, "✅ Товар добавлен (без фото).", kb_main(lang(message.from_user.id)))


@dp.message(AddWizard.photo, F.photo)
async def admin_wizard_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    photo_file_id = message.photo[-1].file_id
    db.add_product(data["category"], data["title"], data["price_cents"], data["stock"], photo_file_id=photo_file_id)
    await state.clear()
    await send_ui(bot, message.chat.id, message.from_user.id, "✅ Товар добавлен с фото.", kb_main(lang(message.from_user.id)))


@dp.callback_query(F.data == "admin:stock")
async def admin_stock(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    cats = db.list_categories()
    lines = []
    for c in cats:
        items = db.list_products(c)
        for pid, title, price, stock in items:
            lines.append(f"#{pid} | {c} | {title} | {money(price)} | x{stock}")

    text = "\n".join(lines) if lines else "Пока нет товаров."
    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, text, kb_back(lang(call.from_user.id), "menu:admin"))


# ---------------- CHANNEL ----------------
@dp.message(F.text == "/setchannel")
async def setchannel_start(message: Message):
    if message.from_user.id != ADMIN_IDS:
        return
    WAITING_CHANNEL.add(message.from_user.id)
    await message.answer("Ок! Перешли любой пост из канала @baerka_shop.")


@dp.message(F.forward_from_chat)
async def catch_forwarded_from_channel(message: Message):
    if message.from_user.id != ADMIN_IDS:
        return
    if message.from_user.id not in WAITING_CHANNEL:
        return
    chat = message.forward_from_chat
    db.set_setting("channel_id", str(chat.id))
    WAITING_CHANNEL.discard(message.from_user.id)
    await message.answer(f"✅ Канал сохранён! ID: {chat.id}")


@dp.message(F.text == "/post")
async def post_to_channel(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return
    channel_id = db.get_setting("channel_id")
    if not channel_id:
        await message.answer("Сначала /setchannel и перешли пост из канала.")
        return

    text = (
        "🇷🇺 *Магазин BÄRKA*\n"
        "Заказ вещей прямо в Telegram — нажми кнопку ниже 👇\n\n"
        "🇩🇪 *BÄRKA Shop*\n"
        "Bestellung direkt in Telegram — klicke auf den Button unten 👇"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Открыть магазин | Shop öffnen", url="https://t.me/baerka_shop_bot")

    await bot.send_message(int(channel_id), text, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await message.answer("✅ Пост отправлен в канал.")







async def cart_expiry_worker(bot: Bot):
    # Каждые 60 сек возвращаем на склад корзины без активности 30 минут
    while True:
        try:
            users = db.stale_cart_users(minutes=30)
            for uid in users:
                db.release_cart(uid)
                try:
                    lg = lang(uid)
                    if lg == 'de':
                        text = '⏱ Warenkorb geleert (30 Min. inaktiv). Artikel sind wieder verfügbar.'
                    else:
                        text = '⏱ Корзина очищена (30 минут без активности). Товары снова в наличии.'
                    await bot.send_message(uid, text)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)







@dp.message(F.text.startswith("/orders"))
async def admin_orders(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return

    parts = message.text.strip().split()
    status = "new"
    if len(parts) >= 2:
        status = parts[1].strip().lower()

    # допускаем только эти статусы
    if status not in ("new", "accepted", "declined", "cancelled"):
        await message.answer("Используй: /orders new | accepted | declined | cancelled")
        return

    orders = db.list_orders(status=status, limit=20)
    if not orders:
        await message.answer(f"✅ Нет заказов со статусом: {status}")
        return

    # Заголовок + кнопка Refresh
    head = f"📦 Заказы: {status.upper()} | {len(orders)} (последние 20)"
    kb_head = InlineKeyboardBuilder()
    kb_head.button(text="🔄 Refresh", callback_data=f"orders:refresh:{status}")
    await bot.send_message(message.chat.id, head, reply_markup=kb_head.as_markup())

    for (oid, user_id, st, tg_username, tg_name, cname, phone, address, pay_method, total_cents, created_at) in orders:
        tg_link = f"tg://user?id={user_id}"
        uname = f"@{tg_username}" if tg_username else "(none)"
        tg_name = tg_name if tg_name else "(empty)"
        cname = cname if cname else "(empty)"

        lines = [
            f"🧾 Order #{oid} | {st.upper()}",
            f"Total: {money(int(total_cents))}",
            f"Created: {created_at}",
            "",
            f"User ID: {user_id}",
            f"TG name: {tg_name}",
            f"Username: {uname}",
            f"Link: {tg_link}",
            "",
            f"Customer name: {cname}",
            f"Phone: {phone}",
            f"Address: {address}",
            f"Pay: {pay_method}",
        ]
        text = "\n".join(lines)

        kb = InlineKeyboardBuilder()

        # NEW заказы можно принимать/отклонять/редактировать/отменять
        if st == "new":
            kb.button(text="✅ Accept", callback_data=f"ord:accept:{oid}")
            kb.button(text="❌ Decline", callback_data=f"ord:decline:{oid}")
            kb.button(text="✏️ Edit", callback_data=f"ord:edit:{oid}")
            kb.button(text="🗑 Cancel", callback_data=f"ord:cancel:{oid}")
            kb.adjust(2, 2)
        else:
            # для других статусов только написать
            kb.adjust(1)

        kb.button(text="💬 Message", url=tg_link)
        kb.adjust(1)

        await bot.send_message(message.chat.id, text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("orders:refresh:"))
async def orders_refresh(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return
    status = call.data.split(":")[2]
    await call.answer("🔄")
    # просто повторно покажем список
    fake = type("obj", (), {})()
    fake.from_user = call.from_user
    fake.chat = call.message.chat
    fake.text = f"/orders {status}"
    await admin_orders(fake, bot)


def order_edit_keyboard(order_id: int):
    items = db.order_items_full(order_id)
    kb = InlineKeyboardBuilder()
    for pid, title, price, qty in items:
        # Кнопки изменения количества одной позиции
        kb.button(text=f"➖ {title}", callback_data=f"orditem:dec:{order_id}:{pid}")
        kb.button(text=f"➕ {title}", callback_data=f"orditem:inc:{order_id}:{pid}")
        kb.adjust(2)
    kb.button(text="🔄 Refresh", callback_data=f"ord:edit:{order_id}")
    kb.button(text="⬅️ Back to /orders", callback_data="orders:back")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data == "orders:back")
async def orders_back(call: CallbackQuery):
    await call.answer("OK")


@dp.callback_query(F.data.startswith("ord:edit:"))
async def admin_order_edit(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    order_id = int(call.data.split(":")[2])
    order = db.get_order(order_id)
    if not order:
        await call.answer("Not found", show_alert=True)
        return

    _id, user_id, status, tg_username, tg_name, cname, phone, address, pay_method, total_cents = order
    if status != "new":
        await call.answer("Можно редактировать только NEW", show_alert=True)
        return

    items = db.order_items_full(order_id)
    lines = [f"✏️ Edit Order #{order_id} (NEW)"]
    for pid, title, price, qty in items:
        lines.append(f"- {title} x{qty} = {money(int(price)*int(qty))}")
    lines.append("")
    lines.append(f"Total: {money(db.recalc_order_total(order_id))}")
    text = "\n".join(lines)

    await call.answer("✏️")
    await bot.send_message(call.message.chat.id, text, reply_markup=order_edit_keyboard(order_id))


@dp.callback_query(F.data.startswith("orditem:"))
async def admin_order_item_change(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    _, act, oid, pid = call.data.split(":")
    order_id = int(oid)
    product_id = int(pid)

    order = db.get_order(order_id)
    if not order:
        await call.answer("Order not found", show_alert=True)
        return
    if order[2] != "new":
        await call.answer("Только NEW можно менять", show_alert=True)
        return

    delta = 1 if act == "inc" else -1
    ok, new_qty, new_total, reason = db.order_item_delta(order_id, product_id, delta)

    if not ok and reason == "no_stock":
        await call.answer("Нет товара на складе", show_alert=True)
        return

    await call.answer("✅")
    # Обновим экран редактирования
    items = db.order_items_full(order_id)
    lines = [f"✏️ Edit Order #{order_id} (NEW)"]
    for pid2, title, price, qty in items:
        lines.append(f"- {title} x{qty} = {money(int(price)*int(qty))}")
    lines.append("")
    lines.append(f"Total: {money(new_total)}")
    text = "\n".join(lines)
    await bot.send_message(call.message.chat.id, text, reply_markup=order_edit_keyboard(order_id))


@dp.callback_query(F.data.startswith("ord:cancel:"))
async def admin_order_cancel(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    order_id = int(call.data.split(":")[2])
    order = db.get_order(order_id)
    if not order:
        await call.answer("Not found", show_alert=True)
        return

    _id, user_id, status, tg_username, tg_name, cname, phone, address, pay_method, total_cents = order
    if status != "new":
        await call.answer("Отменять можно только NEW", show_alert=True)
        return

    db.cancel_order(order_id)

    # уведомим клиента на его языке
    try:
        lg = lang(user_id)
        if lg == "de":
            msg = "❌ Bestellung wurde storniert. Bitte kontaktiere uns, falls du Fragen hast."
        else:
            msg = "❌ Заказ отменён. Если есть вопросы — напиши нам."
        await bot.send_message(user_id, msg)
    except Exception:
        pass

    await call.answer("🗑 Cancelled", show_alert=True)


@dp.message(F.text.startswith("/orders"))
async def admin_orders(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return

    parts = message.text.strip().split()
    status = "new"
    if len(parts) >= 2:
        status = parts[1].strip().lower()

    # допускаем только эти статусы
    if status not in ("new", "accepted", "declined", "cancelled"):
        await message.answer("Используй: /orders new | accepted | declined | cancelled")
        return

    orders = db.list_orders(status=status, limit=20)
    if not orders:
        await message.answer(f"✅ Нет заказов со статусом: {status}")
        return

    # Заголовок + кнопка Refresh
    head = f"📦 Заказы: {status.upper()} | {len(orders)} (последние 20)"
    kb_head = InlineKeyboardBuilder()
    kb_head.button(text="🔄 Refresh", callback_data=f"orders:refresh:{status}")
    await bot.send_message(message.chat.id, head, reply_markup=kb_head.as_markup())

    for (oid, user_id, st, tg_username, tg_name, cname, phone, address, pay_method, total_cents, created_at) in orders:
        tg_link = f"tg://user?id={user_id}"
        uname = f"@{tg_username}" if tg_username else "(none)"
        tg_name = tg_name if tg_name else "(empty)"
        cname = cname if cname else "(empty)"

        lines = [
            f"🧾 Order #{oid} | {st.upper()}",
            f"Total: {money(int(total_cents))}",
            f"Created: {created_at}",
            "",
            f"User ID: {user_id}",
            f"TG name: {tg_name}",
            f"Username: {uname}",
            f"Link: {tg_link}",
            "",
            f"Customer name: {cname}",
            f"Phone: {phone}",
            f"Address: {address}",
            f"Pay: {pay_method}",
        ]
        text = "\n".join(lines)

        kb = InlineKeyboardBuilder()

        # NEW заказы можно принимать/отклонять/редактировать/отменять
        if st == "new":
            kb.button(text="✅ Accept", callback_data=f"ord:accept:{oid}")
            kb.button(text="❌ Decline", callback_data=f"ord:decline:{oid}")
            kb.button(text="✏️ Edit", callback_data=f"ord:edit:{oid}")
            kb.button(text="🗑 Cancel", callback_data=f"ord:cancel:{oid}")
            kb.adjust(2, 2)
        else:
            # для других статусов только написать
            kb.adjust(1)

        kb.button(text="💬 Message", url=tg_link)
        kb.adjust(1)

        await bot.send_message(message.chat.id, text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("orders:refresh:"))
async def orders_refresh(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return
    status = call.data.split(":")[2]
    await call.answer("🔄")
    # просто повторно покажем список
    fake = type("obj", (), {})()
    fake.from_user = call.from_user
    fake.chat = call.message.chat
    fake.text = f"/orders {status}"
    await admin_orders(fake, bot)


def order_edit_keyboard(order_id: int):
    items = db.order_items_full(order_id)
    kb = InlineKeyboardBuilder()
    for pid, title, price, qty in items:
        # Кнопки изменения количества одной позиции
        kb.button(text=f"➖ {title}", callback_data=f"orditem:dec:{order_id}:{pid}")
        kb.button(text=f"➕ {title}", callback_data=f"orditem:inc:{order_id}:{pid}")
        kb.adjust(2)
    kb.button(text="🔄 Refresh", callback_data=f"ord:edit:{order_id}")
    kb.button(text="⬅️ Back to /orders", callback_data="orders:back")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data == "orders:back")
async def orders_back(call: CallbackQuery):
    await call.answer("OK")


@dp.callback_query(F.data.startswith("ord:edit:"))
async def admin_order_edit(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    order_id = int(call.data.split(":")[2])
    order = db.get_order(order_id)
    if not order:
        await call.answer("Not found", show_alert=True)
        return

    _id, user_id, status, tg_username, tg_name, cname, phone, address, pay_method, total_cents = order
    if status != "new":
        await call.answer("Можно редактировать только NEW", show_alert=True)
        return

    items = db.order_items_full(order_id)
    lines = [f"✏️ Edit Order #{order_id} (NEW)"]
    for pid, title, price, qty in items:
        lines.append(f"- {title} x{qty} = {money(int(price)*int(qty))}")
    lines.append("")
    lines.append(f"Total: {money(db.recalc_order_total(order_id))}")
    text = "\n".join(lines)

    await call.answer("✏️")
    await bot.send_message(call.message.chat.id, text, reply_markup=order_edit_keyboard(order_id))


@dp.callback_query(F.data.startswith("orditem:"))
async def admin_order_item_change(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    _, act, oid, pid = call.data.split(":")
    order_id = int(oid)
    product_id = int(pid)

    order = db.get_order(order_id)
    if not order:
        await call.answer("Order not found", show_alert=True)
        return
    if order[2] != "new":
        await call.answer("Только NEW можно менять", show_alert=True)
        return

    delta = 1 if act == "inc" else -1
    ok, new_qty, new_total, reason = db.order_item_delta(order_id, product_id, delta)

    if not ok and reason == "no_stock":
        await call.answer("Нет товара на складе", show_alert=True)
        return

    await call.answer("✅")
    # Обновим экран редактирования
    items = db.order_items_full(order_id)
    lines = [f"✏️ Edit Order #{order_id} (NEW)"]
    for pid2, title, price, qty in items:
        lines.append(f"- {title} x{qty} = {money(int(price)*int(qty))}")
    lines.append("")
    lines.append(f"Total: {money(new_total)}")
    text = "\n".join(lines)
    await bot.send_message(call.message.chat.id, text, reply_markup=order_edit_keyboard(order_id))


@dp.callback_query(F.data.startswith("ord:cancel:"))
async def admin_order_cancel(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    order_id = int(call.data.split(":")[2])
    order = db.get_order(order_id)
    if not order:
        await call.answer("Not found", show_alert=True)
        return

    _id, user_id, status, tg_username, tg_name, cname, phone, address, pay_method, total_cents = order
    if status != "new":
        await call.answer("Отменять можно только NEW", show_alert=True)
        return

    db.cancel_order(order_id)

    # уведомим клиента на его языке
    try:
        lg = lang(user_id)
        if lg == "de":
            msg = "❌ Bestellung wurde storniert. Bitte kontaktiere uns, falls du Fragen hast."
        else:
            msg = "❌ Заказ отменён. Если есть вопросы — напиши нам."
        await bot.send_message(user_id, msg)
    except Exception:
        pass

    await call.answer("🗑 Cancelled", show_alert=True)




@dp.message(F.text == "/products")
async def admin_products(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return

    cats = db.list_categories()
    if not cats:
        await message.answer("Пока нет товаров.")
        return

    kb = InlineKeyboardBuilder()
    for c in cats:
        kb.button(text=c, callback_data=f"admcat:{c}")
    kb.adjust(1)
    await bot.send_message(message.chat.id, "📦 Категории:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("admcat:"))
async def admin_cat_open(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    cat = call.data.split(":", 1)[1]
    items = db.products_by_category(cat)

    kb = InlineKeyboardBuilder()
    for pid, title, price, stock in items:
        kb.button(text=f"{title} | {money(int(price))} | x{stock}", callback_data=f"prod:open:{pid}")
    kb.button(text="⬅️ Назад", callback_data="prod:backcats")
    kb.adjust(1)

    await call.answer()
    await bot.send_message(call.message.chat.id, f"📦 {cat}:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "prod:backcats")
async def admin_back_cats(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    await call.answer()
    # дальше твой код...
    fake = type("obj", (), {})()
    fake.from_user = call.from_user
    fake.chat = call.message.chat
    fake.text = "/products"
    await admin_products(fake, bot)


def admin_product_kb(pid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕1", callback_data=f"prod:delta:{pid}:1")
    kb.button(text="➖1", callback_data=f"prod:delta:{pid}:-1")
    kb.button(text="➕5", callback_data=f"prod:delta:{pid}:5")
    kb.button(text="➖5", callback_data=f"prod:delta:{pid}:-5")
    kb.adjust(4)
    kb.button(text="✍️ Set stock", callback_data=f"prod:setstock:{pid}")
    kb.button(text="💶 Set price", callback_data=f"prod:setprice:{pid}")
    kb.adjust(2)
    kb.button(text="🗑 Delete", callback_data=f"prod:delask:{pid}")
    kb.button(text="⬅️ Back", callback_data="prod:backcats")
    kb.adjust(2)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("prod:open:"))
async def admin_prod_open(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    pid = int(call.data.split(":")[2])
    p = db.get_product(pid)
    if not p:
        await call.answer("Not found", show_alert=True)
        return

    _id, cat, title, price, stock, photo = p
    lines = [
        f"🧾 Product #{pid}",
        f"Category: {cat}",
        f"Title: {title}",
        f"Price: {money(int(price))}",
        f"Stock: {stock}",
    ]
    text = "\n".join(lines)

    await call.answer()
    await bot.send_message(call.message.chat.id, text, reply_markup=admin_product_kb(pid))


@dp.callback_query(F.data.startswith("prod:delta:"))
async def admin_prod_delta(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    _, _, pid, delta = call.data.split(":")
    pid = int(pid)
    delta = int(delta)

    new_stock = db.product_stock_delta(pid, delta)
    if new_stock < 0:
        await call.answer("Not found", show_alert=True)
        return

    await call.answer(f"Stock: {new_stock}", show_alert=True)

    fake = type("obj", (), {})()
    fake.from_user = call.from_user
    fake.message = call.message
    fake.data = f"prod:open:{pid}"
    await admin_prod_open(fake, bot)


@dp.callback_query(F.data.startswith("prod:setstock:"))
async def admin_prod_setstock(call: CallbackQuery, state: FSMContext, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return
    pid = int(call.data.split(":")[2])
    await state.set_state(ProductEdit.set_stock)
    await state.update_data(pid=pid)
    await call.answer()
    await bot.send_message(call.message.chat.id, "Введи новый остаток (число):")


@dp.message(ProductEdit.set_stock)
async def admin_prod_setstock_value(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return
    data = await state.get_data()
    pid = int(data.get("pid"))
    try:
        stock = int(message.text.strip())
    except Exception:
        await message.answer("Нужно число. Пример: 50")
        return

    db.product_set_stock(pid, stock)
    await state.clear()
    await message.answer(f"✅ Stock установлен: {max(0, stock)}")

    fake = type("obj", (), {})()
    fake.from_user = message.from_user
    fake.message = type("m", (), {"chat": message.chat})()
    fake.data = f"prod:open:{pid}"
    await admin_prod_open(fake, bot)


@dp.callback_query(F.data.startswith("prod:setprice:"))
async def admin_prod_setprice(call: CallbackQuery, state: FSMContext, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return
    pid = int(call.data.split(":")[2])
    await state.set_state(ProductEdit.set_price)
    await state.update_data(pid=pid)
    await call.answer()
    await bot.send_message(call.message.chat.id, "Введи новую цену (например 19.99):")


@dp.message(ProductEdit.set_price)
async def admin_prod_setprice_value(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_IDS:
        return
    data = await state.get_data()
    pid = int(data.get("pid"))

    try:
        val = message.text.strip().replace(",", ".")
        price_cents = int(round(float(val) * 100))
    except Exception:
        await message.answer("Не понял цену. Пример: 19.99")
        return

    db.product_set_price(pid, price_cents)
    await state.clear()
    await message.answer(f"✅ Цена установлена: {money(price_cents)}")

    fake = type("obj", (), {})()
    fake.from_user = message.from_user
    fake.message = type("m", (), {"chat": message.chat})()
    fake.data = f"prod:open:{pid}"
    await admin_prod_open(fake, bot)


@dp.callback_query(F.data.startswith("prod:delask:"))
async def admin_prod_delask(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    pid = int(call.data.split(":")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ YES, delete", callback_data=f"prod:delyes:{pid}")
    kb.button(text="❌ NO", callback_data=f"prod:open:{pid}")
    kb.adjust(2)
    await call.answer()
    await bot.send_message(call.message.chat.id, f"Точно удалить товар #{pid}?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("prod:delyes:"))
async def admin_prod_delyes(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    pid = int(call.data.split(":")[2])
    db.product_delete(pid)
    await call.answer("Deleted", show_alert=True)
    await bot.send_message(call.message.chat.id, f"🗑 Товар #{pid} удалён.")


@dp.callback_query(F.data.startswith("admin:"))
async def admin_router(call: CallbackQuery, bot: Bot):
    # единая точка входа для всех кнопок админки
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("No access", show_alert=True)
        return

    data = call.data

    try:
        if data.startswith("admin:orders:"):
            status = data.split(":")[2]
            await call.answer("OK")
            fake = type("obj", (), {})()
            fake.from_user = call.from_user
            fake.chat = call.message.chat
            fake.text = f"/orders {status}"
            await admin_orders(fake, bot)
            return

        if data == "admin:products":
            await call.answer("OK")
            fake = type("obj", (), {})()
            fake.from_user = call.from_user
            fake.chat = call.message.chat
            fake.text = "/products"
            await admin_products(fake, bot)
            return

        if data == "admin:setchannel":
            await call.answer("OK")
            WAITING_CHANNEL.add(call.from_user.id)
            await bot.send_message(call.message.chat.id, "Ок! Перешли любой пост из канала, чтобы сохранить канал.")
            return

        if data == "admin:post":
            await call.answer("OK")
            fake = type("obj", (), {})()
            fake.from_user = call.from_user
            fake.chat = call.message.chat
            fake.text = "/post"
            await post_to_channel(fake, bot)
            return

        await call.answer("Unknown admin action", show_alert=True)

    except Exception as e:
        # покажем ошибку тебе, чтобы быстро чинить
        await call.answer("Error", show_alert=True)
        try:
            await bot.send_message(ADMIN_IDS, f"❗ Admin button error: {e}")
        except Exception:
            pass

async def main():
    db.init_db()
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(cart_expiry_worker(bot))

    # --- Render keep-alive (чтобы Render видел порт и не падал) ---
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    # -------------------------------------------------------------

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
