import asyncio
import os
from typing import Iterable, Optional, Union

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from config import BOT_TOKEN, ADMIN_IDS, CURRENCY
import db
from texts import TEXT


# ===================== helpers =====================

def is_admin(user_id: int) -> bool:
    """
    ADMIN_IDS может быть:
    - int (один админ)
    - list/set/tuple (несколько админов)
    """
    if ADMIN_IDS is None:
        return False
    if isinstance(ADMIN_IDS, int):
        return user_id == ADMIN_IDS
    try:
        return user_id in ADMIN_IDS
    except TypeError:
        return user_id == ADMIN_IDS


def currency_symbol(code: str) -> str:
    code = (code or "").upper()
    if code == "EUR":
        return "€"
    if code == "USD":
        return "$"
    if code == "RUB":
        return "₽"
    return code


def money(cents: Union[int, float]) -> str:
    """
    В проекте цены в БД у тебя идут как "центы" (price_cents).
    Поэтому всегда форматируем как euros = cents/100.
    """
    sym = currency_symbol(CURRENCY)
    try:
        euros = float(cents) / 100.0
    except Exception:
        return f"{cents} {sym}"
    return f"{euros:.2f} {sym}"


# ===================== states =====================

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


# ===================== app globals =====================

dp = Dispatcher()
USER_LANG = {}
WAITING_CHANNEL = set()
LAST_UI_MSG = {}  # user_id -> message_id


def lang(user_id: int) -> str:
    if user_id in USER_LANG:
        return USER_LANG[user_id]
    saved = db.get_setting(f"lang:{user_id}")
    if saved in ("ru", "de"):
        USER_LANG[user_id] = saved
        return saved
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


# ===================== START / LANG =====================

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


# ===================== CATALOG =====================

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
    for pid, title, price_cents, stock in products:
        kb.button(text=f"{title} — {money(price_cents)} (x{stock})", callback_data=f"p:{pid}")
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

    _id, category, title, price_cents, stock, photo_file_id = p
    total_qty = cart_total_qty(call.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ 1", callback_data=f"add:{pid}:1")
    kb.button(text="➕ 2", callback_data=f"add:{pid}:2")
    kb.button(text="➕ 5", callback_data=f"add:{pid}:5")
    kb.adjust(3)
    kb.button(text=f"{TEXT['cart'][lg]} ({total_qty})", callback_data="menu:cart")
    kb.button(text=TEXT["back"][lg], callback_data=f"cat:{category}")
    kb.adjust(2)

    caption = f"{title}\n{money(price_cents)}\nStock: {stock}"

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, caption, kb.as_markup(), photo=photo_file_id)


# ===================== CART =====================

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
        msg = (f"✅ Hinzugefügt: +{added}\n🧺 Im Warenkorb: {total_qty}"
               if lg == "de"
               else f"✅ Добавлено: +{added}\n🧺 В корзине: {total_qty}")

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

    total_cents = 0
    lines = []
    kb = InlineKeyboardBuilder()

    for pid, title, price_cents, qty in items:
        total_cents += int(price_cents) * int(qty)
        lines.append(f"• {title} × {qty} = {money(int(price_cents) * int(qty))}")
        kb.button(text=f"➖ 1 {title}", callback_data=f"rm1:{pid}")

    text = "\n".join(lines) + f"\n\nTotal: {money(total_cents)}"

    kb.button(text=TEXT["checkout"][lg], callback_data="checkout:start")
    kb.button(text=TEXT["clear_cart"][lg], callback_data="cart:clear")
    kb.button(text=TEXT["continue_shop"][lg], callback_data="menu:catalog")
    kb.adjust(1)

    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, text, kb.as_markup())


# ===================== CHECKOUT =====================

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

    order_id, total_cents, items = created

    await call.answer("✅")
    await state.clear()

    await send_ui(
        bot, call.message.chat.id, call.from_user.id,
        TEXT["order_done"][lg] + f"\nOrder #{order_id}\nTotal: {money(total_cents)}",
        kb_main(lg)
    )

    # уведомление админу (первому админу если список)
    admin_target = ADMIN_IDS
    if not isinstance(admin_target, int):
        admin_target = list(admin_target)[0] if admin_target else None

    if admin_target:
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
        for pid, title, price_cents, qty in items:
            lines.append(f"- {title} x{qty} = {money(int(price_cents) * int(qty))}")
        lines.append(f"\nTOTAL: {money(total_cents)}")

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять / Accept", callback_data=f"ord:accept:{order_id}")
        kb.button(text="❌ Отклонить / Decline", callback_data=f"ord:decline:{order_id}")
        kb.adjust(2)
        kb.button(text="💬 Написать клиенту / Message", url=tg_link)
        kb.adjust(1)

        await bot.send_message(int(admin_target), "\n".join(lines), reply_markup=kb.as_markup())


# ===================== ADMIN: accept/decline =====================

@dp.callback_query(F.data.startswith("ord:"))
async def admin_order_action(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
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
            await bot.send_message(user_id, "✅ Ваш заказ подтверждён! Мы скоро свяжемся с вами.")
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
            await bot.send_message(user_id, "❌ К сожалению, заказ отклонён. Напишите нам, чтобы уточнить детали.")
        except Exception:
            pass
        await call.answer("❌ Отклонено", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return


# ===================== ADMIN MENU + WIZARD + STOCK =====================

@dp.callback_query(F.data == "menu:admin")
async def admin_entry(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Список остатков", callback_data="admin:stock")
    kb.button(text="➕ Добавить товар", callback_data="admin:wizard")
    kb.adjust(1)

    kb.button(text="🔗 Привязать канал (/setchannel)", callback_data="admin:setchannel")
    kb.button(text="📢 Пост в канал (/post)", callback_data="admin:post")
    kb.adjust(1)

    kb.button(text="⬅️ Назад", callback_data="menu:root")
    kb.adjust(1)

    await call.answer()
    await bot.send_message(call.message.chat.id, "⚙️ Админка:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "admin:wizard")
async def admin_wizard_start(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user.id):
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
    if not is_admin(message.from_user.id):
        return
    await state.update_data(category=message.text.strip())
    await state.set_state(AddWizard.title)
    await send_ui(bot, message.chat.id, message.from_user.id, "Название товара?", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.title)
async def admin_wizard_title(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AddWizard.price)
    await send_ui(bot, message.chat.id, message.from_user.id, "Цена (например 19.99):", kb_cancel_to(lang(message.from_user.id), "menu:admin"))


@dp.message(AddWizard.price)
async def admin_wizard_price(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
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
    if not is_admin(message.from_user.id):
        return
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
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    db.add_product(data["category"], data["title"], data["price_cents"], data["stock"], photo_file_id=None)
    await state.clear()
    await send_ui(bot, message.chat.id, message.from_user.id, "✅ Товар добавлен (без фото).", kb_main(lang(message.from_user.id)))


@dp.message(AddWizard.photo, F.photo)
async def admin_wizard_photo(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    photo_file_id = message.photo[-1].file_id
    db.add_product(data["category"], data["title"], data["price_cents"], data["stock"], photo_file_id=photo_file_id)
    await state.clear()
    await send_ui(bot, message.chat.id, message.from_user.id, "✅ Товар добавлен с фото.", kb_main(lang(message.from_user.id)))


@dp.callback_query(F.data == "admin:stock")
async def admin_stock(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    cats = db.list_categories()
    lines = []
    for c in cats:
        items = db.list_products(c)
        for pid, title, price_cents, stock in items:
            lines.append(f"#{pid} | {c} | {title} | {money(price_cents)} | x{stock}")

    text = "\n".join(lines) if lines else "Пока нет товаров."
    await call.answer()
    await send_ui(bot, call.message.chat.id, call.from_user.id, text, kb_back(lang(call.from_user.id), "menu:admin"))


# ===================== CHANNEL =====================

@dp.message(F.text == "/setchannel")
async def setchannel_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    WAITING_CHANNEL.add(message.from_user.id)
    await message.answer("Ок! Перешли любой пост из канала, чтобы я сохранил channel_id.")


@dp.message(F.forward_from_chat)
async def catch_forwarded_from_channel(message: Message):
    if not is_admin(message.from_user.id):
        return
    if message.from_user.id not in WAITING_CHANNEL:
        return
    chat = message.forward_from_chat
    db.set_setting("channel_id", str(chat.id))
    WAITING_CHANNEL.discard(message.from_user.id)
    await message.answer(f"✅ Канал сохранён! ID: {chat.id}")


@dp.message(F.text == "/post")
async def post_to_channel(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
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


# ===================== background worker =====================

async def cart_expiry_worker(bot: Bot):
    while True:
        try:
            users = db.stale_cart_users(minutes=30)
            for uid in users:
                db.release_cart(uid)
                try:
                    lg = lang(uid)
                    text = ("⏱ Warenkorb geleert (30 Min. inaktiv). Artikel sind wieder verfügbar."
                            if lg == "de"
                            else "⏱ Корзина очищена (30 минут без активности). Товары снова в наличии.")
                    await bot.send_message(uid, text)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)


# ===================== admin router =====================

@dp.callback_query(F.data.startswith("admin:"))
async def admin_router(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    data = call.data
    await call.answer("OK")

    if data == "admin:setchannel":
        WAITING_CHANNEL.add(call.from_user.id)
        await bot.send_message(call.message.chat.id, "Ок! Перешли пост из канала, чтобы сохранить канал.")
        return

    if data == "admin:post":
        fake = type("obj", (), {})()
        fake.from_user = call.from_user
        fake.chat = call.message.chat
        fake.text = "/post"
        await post_to_channel(fake, bot)
        return

    if data == "admin:stock":
        await admin_stock(call, bot)
        return

    if data == "admin:wizard":
        # сюда не попадём (есть отдельный хэндлер), но пусть будет безопасно
        return

    await bot.send_message(call.message.chat.id, f"Unknown admin action: {data}")


# ===================== Render keep-alive =====================

async def start_health_server() -> web.AppRunner:
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Health server running on port {port}")
    return runner


# ===================== MAIN =====================

async def main():
    db.init_db()

    bot = Bot(BOT_TOKEN)

    # фоновые задачи
    asyncio.create_task(cart_expiry_worker(bot))

    # Render: держим порт
    await start_health_server()

    # важно для ошибки 409: выключаем webhook и включаем polling
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
