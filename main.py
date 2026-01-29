import asyncio
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from config import BOT_TOKEN, ADMIN_IDS, CURRENCY
import db
from texts import TEXT


# ----------------- MONEY -----------------
def currency_symbol(code: str) -> str:
    code = (code or "").upper()
    if code == "EUR":
        return "€"
    if code == "USD":
        return "$"
    if code == "RUB":
        return "₽"
    return code


def money(cents: int | float) -> str:
    # у тебя все цены в базе в cents
    euros = float(cents) / 100
    return f"{euros:.2f} {currency_symbol(CURRENCY)}"


# ----------------- STATES -----------------
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


# ----------------- GLOBALS -----------------
dp = Dispatcher()
USER_LANG = {}
WAITING_CHANNEL = set()
LAST_UI_MSG = {}  # user_id -> message_id


# ----------------- LANG -----------------
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


# ---------------- START / LANG ----------------
@dp.message(F.text.in_({"/start", "start"}))
async def start(message: Message, bot: Bot):
    await send_ui(
        bot, message.chat.id, message.from_user.id,
        TEXT["choose_lang"]["ru"] + "\n" + TEXT["choose_lang"]["de"],
        kb_lang()
    )


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


# ---------------- CART ----------------
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
        msg = f"✅ Добавлено: +{added}\n🧺 В корзине: {total_qty}" if lg == "ru" else f"✅ Hinzugefügt: +{added}\n🧺 Im Warenkorb: {total_qty}"
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

    if ADMIN_IDS:
        tg_link = f"tg://user?id={call.from_user.id}"

        lines = [
            f"🧾 New order #{order_id}",
            f"Status: NEW",
            f"User ID: {call.from_user.id}",
            f"TG name: {tg_name if tg_name else '(empty)'}",
            f"Username: @{tg_username}" if tg_username else "Username: (none)",
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
        kb.button(text="💬 Написать клиенту / Message", url=tg_link)
        kb.adjust(1)

        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, "\n".join(lines), reply_markup=kb.as_markup())


# ---------------- ADMIN accept/decline ----------------
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

    _id, user_id, status, tg_username, tg_name, cname, phone, address, pay_method, total_cents = order

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


# ---------------- BACKGROUND ----------------
async def cart_expiry_worker(bot: Bot):
    while True:
        try:
            users = db.stale_cart_users(minutes=30)
            for uid in users:
                db.release_cart(uid)
                try:
                    lg = lang(uid)
                    text = (
                        "⏱ Корзина очищена (30 минут без активности). Товары снова в наличии."
                        if lg == "ru"
                        else "⏱ Warenkorb geleert (30 Min. inaktiv). Artikel sind wieder verfügbar."
                    )
                    await bot.send_message(uid, text)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)


# ---------------- WEB SERVER (Render) ----------------
async def start_web_server():
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Web server running on port {port}")


# ---------------- MAIN ----------------
async def main():
    db.init_db()

    bot = Bot(BOT_TOKEN)

    # Render keep-alive server
    await start_web_server()

    # background tasks
    asyncio.create_task(cart_expiry_worker(bot))

    # IMPORTANT: remove webhook to avoid 409 conflict
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
