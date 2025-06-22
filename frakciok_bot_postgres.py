import discord
from discord.ext import commands, tasks
from discord import ButtonStyle
from discord.ui import Button, View, Select, Modal, TextInput
import psycopg2
import sqlite3 
from datetime import datetime, timedelta, date, time
import os
from dotenv import load_dotenv
from discord.ext.commands import CommandError
import asyncio
import logging
import discord.utils

# Naplózás beállítása
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_log.txt", encoding="utf-8")
    ]
)
logger = logging.getLogger("frakcio_bot")

# A load_dotenv() után adjunk hozzá egy ellenőrzést a Railway környezeti változókhoz
# Környezeti változók betöltése
load_dotenv()

# Ellenőrizzük a BOT_TOKEN környezeti változót és adjunk részletes hibaüzenetet
BOT_TOKEN = os.getenv("BOT_TOKEN")
if BOT_TOKEN is None:
    logger.critical("BOT_TOKEN környezeti változó nem található! A bot nem tud elindulni.")
    logger.info("Környezeti változók ellenőrzése:")
    # Kilistázzuk az összes környezeti változót (csak a neveket, értékek nélkül)
    for key in os.environ.keys():
        logger.info(f"  - {key}")
    logger.info("Ellenőrizd, hogy a BOT_TOKEN környezeti változó be van-e állítva a Railway platformon.")

# Segédfüggvény a lekérdezések végrehajtásához, amely kezeli a különböző adatbázis típusokat
def execute_query(cursor, query, params=None):
    global is_sqlite
    try:
        if is_sqlite:
            # SQLite esetén cseréljük a %s helyőrzőket ? karakterekre
            modified_query = query.replace("%s", "?")
            if params:
                cursor.execute(modified_query, params)
            else:
                cursor.execute(modified_query)
        else:
            # PostgreSQL esetén használjuk az eredeti lekérdezést
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
        return cursor
    except Exception as e:
        logger.error(f"Hiba a lekérdezés végrehajtásakor: {e}, Query: {query}, Params: {params}")
        raise

# Globális változó a bot üzeneteinek nyomon követéséhez
# Maximum 100 üzenetet tárolunk
bot_messages = []

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True  # Reakciók figyelése
intents.members = True # Tagok figyelése

bot = commands.Bot(command_prefix='$', intents=intents)

# Távolítsuk el a beépített help parancsot
bot.remove_command('help')

# Adatbázis kapcsolat
DATABASE_URL = os.getenv("DATABASE_URL")

# Kapcsolat ellenőrző függvény
def ensure_connection():
    global conn, cursor, is_sqlite
    try:
        # Ellenőrizzük, hogy a kapcsolat él-e
        if is_sqlite:
            cursor.execute("SELECT 1")
        else:
            cursor.execute("SELECT 1")
    except (psycopg2.OperationalError, psycopg2.InterfaceError, NameError, sqlite3.OperationalError):
        # Újracsatlakozás vagy első kapcsolódás
        logger.info("Újracsatlakozás az adatbázishoz...")
        if is_sqlite:
            conn = sqlite3.connect('frakciok.db')
        else:
            conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cursor = conn.cursor()
    return conn, cursor

def handle_transaction_error():
    """Kezeli a tranzakciós hibákat, visszagörgeti a tranzakciót ha szükséges."""
    global conn
    try:
        if conn:
            conn.rollback()
            logger.info("Tranzakció visszagörgetve")
    except Exception as e:
        logger.error(f"Hiba a tranzakció visszagörgetésekor: {e}")

# Kezdeti kapcsolat létrehozása
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    logger.info("Sikeres kapcsolódás a PostgreSQL adatbázishoz.")
    is_sqlite = False
except Exception as e:
    logger.error(f"Hiba a PostgreSQL adatbázis kapcsolódás során: {e}")
    logger.info(f"DATABASE_URL értéke: {DATABASE_URL}")
    
    # Próbáljuk meg SQLite-ot használni fallback-ként
    try:
        logger.info("Átváltás SQLite adatbázisra...")
        conn = sqlite3.connect('frakciok.db')
        cursor = conn.cursor()
        
        # SQLite-ban nincs SERIAL típus, helyette AUTOINCREMENT-et használunk
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS frakciok (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nev TEXT UNIQUE NOT NULL,
            kod TEXT NOT NULL,
            kezdet_datum TEXT NOT NULL,
            lejarat_datum TEXT NOT NULL,
            hozzaado_nev TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS auto_frissites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            csatorna_id INTEGER UNIQUE NOT NULL,
            cim_uzenet_id INTEGER,
            hamarosan_uzenet_id INTEGER,
            aktiv_uzenet_id INTEGER,
            lejart_uzenet_id INTEGER,
            aktiv INTEGER DEFAULT 1,
            utolso_frissites TEXT
        )
        ''')
        conn.commit()
        logger.info("SQLite adatbázis sikeresen létrehozva.")
        
        # Ha SQLite-ot használunk, akkor ne próbáljuk meg létrehozni a PostgreSQL táblákat
        is_sqlite = True
    except Exception as sqlite_error:
        logger.critical(f"Hiba az SQLite adatbázis létrehozásakor: {sqlite_error}")
        raise Exception("Nem sikerült kapcsolódni sem PostgreSQL, sem SQLite adatbázishoz.")

# Tábla létrehozása (ha még nem létezik) - csak PostgreSQL esetén
if not is_sqlite:
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS frakciok (
        id SERIAL PRIMARY KEY,
        nev TEXT UNIQUE NOT NULL,
        kod TEXT NOT NULL,
        kezdet_datum TEXT NOT NULL,
        lejarat_datum TEXT NOT NULL,
        hozzaado_nev TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    )
    ''')

    # Új tábla az automatikus üzenetek beállításaihoz
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS auto_frissites (
      id SERIAL PRIMARY KEY,
      csatorna_id BIGINT UNIQUE NOT NULL,
      cim_uzenet_id BIGINT,
      hamarosan_uzenet_id BIGINT,
      aktiv_uzenet_id BIGINT,
      lejart_uzenet_id BIGINT,
      aktiv BOOLEAN DEFAULT TRUE,
      utolso_frissites TIMESTAMP WITH TIME ZONE
    )
    ''')
    
    # Ellenőrizzük, hogy létezik-e az utolso_frissites oszlop
    try:
        # Használjunk információs sémát a column ellenőrzésére
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='auto_frissites' AND column_name='utolso_frissites'
        """)
        column_exists = cursor.fetchone() is not None
        
        if not column_exists:
            # Ha nem létezik, hozzáadjuk
            logger.info("utolso_frissites oszlop hozzáadása az auto_frissites táblához")
            cursor.execute("ALTER TABLE auto_frissites ADD COLUMN utolso_frissites TIMESTAMP WITH TIME ZONE")
            conn.commit()
    except Exception as e:
        # Hiba esetén rollback és újrapróbálkozás
        conn.rollback()
        logger.error(f"Hiba az utolso_frissites oszlop ellenőrzésekor: {e}")
        try:
            # Próbáljuk meg közvetlenül hozzáadni az oszlopot
            cursor.execute("ALTER TABLE auto_frissites ADD COLUMN IF NOT EXISTS utolso_frissites TIMESTAMP WITH TIME ZONE")
            conn.commit()
        except Exception as e2:
            logger.error(f"Nem sikerült hozzáadni az utolso_frissites oszlopot: {e2}")
            conn.rollback()
    
    conn.commit()

# Create the server_settings table if it doesn't exist
if not is_sqlite:
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS server_settings (
        id SERIAL PRIMARY KEY,
        guild_id BIGINT UNIQUE NOT NULL,
        notification_channel_id BIGINT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    )
    ''')
else:
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS server_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER UNIQUE NOT NULL,
        notification_channel_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
conn.commit()

# Parancs aliasok és leírások - minden alias csak egyszer szerepelhet
PARANCSOK = {
    "HOZZAAD": ["add", "new"],
    "LISTA": ["list", "osszes", "all"],
    "KERES": ["search", "talal", "find"],
    "FRISSIT": ["update", "extend"],
    "TOROL": ["delete", "eltavolit", "remove"],
    "SZERKESZT": ["edit", "modosit", "modify"],
    "MENU": ["fomenu", "main", "fo"],
    "SEGITSEG": ["info", "parancsok"],
    "TOROL_UZENET": ["delete_message"],
    "AUTO_FRISSITES": ["auto_lista", "auto_update", "auto_frissites_beallitas"],
    "AUTO_KIKAPCSOLAS": ["auto_off", "auto_disable", "automatikus_frissites_kikapcsolasa"],
    "TESZT_FRISSITES": ["test_update", "teszt_update"],
    "AUTO_TEST": ["test_auto", "auto_teszt"]
}

# Parancsok részletes leírása
PARANCS_LEIRASOK = {
    "HOZZAAD": "Új frakció hozzáadása az adatbázishoz. Használat: `$uj_frakcio` - Interaktív űrlapot nyit.",
    "LISTA": "Az összes frakció listázása kategóriákba rendezve (aktív, hamarosan lejáró, lejárt). Használat: `$lista`",
    "KERES": "Egy adott frakció részletes adatainak megjelenítése. Használat: `$keres [frakció neve]`",
    "FRISSIT": "Frakció szerződésének meghosszabbítása. Használat: `$hosszabbit [frakció neve] [napok] [hetek] [konkrét dátum]` - Csak az egyik paramétert add meg.",
    "TOROL": "Frakció törlése az adatbázisból. Használat: `$torol [frakció neve]`",
    "SZERKESZT": "Frakció adatainak módosítása. Használat: `$szerkeszt [eredeti név] [új név] [új kód]`",
    "MENU": "Főmenü megnyitása, ahonnan az összes funkció elérhető. Használat: `$menu`",
    "SEGITSEG": "Parancsok listájának és leírásának megjelenítése. Használat: `$help` vagy `$segitseg`",
    "TOROL_UZENET": "Bot által küldött üzenetek törlése. Használat: `$purge_bot [szám]` - Alapértelmezetten 1 üzenetet töröl.",
    "AUTO_FRISSITES": "Automatikus napi frissítés beállítása az aktuális vagy megadott csatornán. Használat: `$auto_frissites_beallitas [csatorna]`",
    "AUTO_KIKAPCSOLAS": "Automatikus napi frissítés kikapcsolása egy csatornán. Használat: `$auto_frissites_kikapcsolas [csatorna]`",
    "TESZT_FRISSITES": "Teszt frissítés indítása az aktuális csatornán. Használat: `$teszt_frissites`",
    "AUTO_TEST": "Automatikus frissítés azonnali indítása (normál esetben naponta 14:00-kor fut). Használat: `$auto_teszt_inditas`"
}

# Segédfüggvény a hibák formázásához - javított escape szekvencia
def format_error(error):
    # Használjunk nyers stringet (r prefix) az escape problémák elkerülésének elkerülésére
    return r"\`\`\`" + f"\n{str(error)}\n" + r"\`\`\`"

# Jogosultság ellenőrző függvény
def has_required_role(user):
    if not isinstance(user, discord.Member):
        return False
    
    # Speciális felhasználó azonosító ellenőrzése - mindig hozzáférést kap
    if user.id == 416698130050973718:
        return True
    
    # Ellenőrizzük, hogy a felhasználó rendelkezik-e a "Snr. Buns" ranggal
    return any(role.name == "Snr. Buns" for role in user.roles)

# Jogosultság ellenőrző dekorátor parancsokhoz
def check_role():
    async def predicate(ctx):
        if has_required_role(ctx.author):
            return True
        else:
            embed = discord.Embed(
                title="Hozzáférés megtagadva",
                description="Nincs jogosultságod használni ezt a parancsot. A parancs használatához 'Snr. Buns' rang szükséges.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False
    return commands.check(predicate)

# Üzenet küldési segédfüggvény, amely nyomon követi a bot üzeneteit
async def send_tracked_message(ctx, content=None, *, embed=None, view=None):
    global bot_messages
    
    message = await ctx.send(content=content, embed=embed, view=view)
    
    # Adjunk hozzá egy törlés reakciót
    await message.add_reaction("🗑️")
    
    # Tároljuk el az üzenetet a globális listában
    bot_messages.append(message)
    
    # Ha túl sok üzenet van a listában, távolítsuk el a legrégebbi üzeneteket
    if len(bot_messages) > 100:
        bot_messages.pop(0)
    
    return message

# Interaction válasz nyomon követése
async def track_interaction_response(interaction, message):
    global bot_messages
    
    # Adjunk hozzá egy törlés reakciót
    try:
        await message.add_reaction("🗑️")
    except:
        pass
    
    # Tároljuk el az üzenetet a globális listában
    bot_messages.append(message)
    
    # Ha túl sok üzenet van a listában, távolítsuk el a legrégebbi üzeneteket
    if len(bot_messages) > 100:
        bot_messages.pop(0)

@bot.event
async def on_ready():
    logger.info(f'Bejelentkezve mint {bot.user}')
    bot.conn = conn  # Adatbázis kapcsolat hozzáadása a bothoz
    
    # Ütemezett feladat indítása
    if not napi_frissites.is_running():
        napi_frissites.start()
        logger.info("Napi frissítés ütemezett feladat elindítva")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        embed = discord.Embed(title="Hiba", description="A bot nem találja az általad beírt parancsot, vagy az nem létezik.", color=discord.Color.red())
        await send_tracked_message(ctx, embed=embed)
    elif isinstance(error, commands.CheckFailure):
        # Ez a hiba már kezelve van a check_role() dekorátorban
        pass
    else:
        logger.error(f"Parancs hiba: {error}")
        embed = discord.Embed(title="Hiba", description=f"Hiba történt: {error}", color=discord.Color.red())
        await send_tracked_message(ctx, embed=embed)

# Reakció figyelő esemény
@bot.event
async def on_reaction_add(reaction, user):
    # Ignoráljuk a bot saját reakcióit
    if user.bot:
        return
    
    # Ellenőrizzük, hogy a reakció egy törlés emoji-e
    if str(reaction.emoji) == "🗑️":
        message = reaction.message
        
        # Ellenőrizzük, hogy a bot üzenete-e
        if message.author.id == bot.user.id:
            # Ellenőrizzük, hogy a felhasználónak van-e jogosultsága törölni
            if has_required_role(user):
                try:
                    await message.delete()
                    # Távolítsuk el az üzenetet a listából
                    if message in bot_messages:
                        bot_messages.remove(message)
                except discord.errors.NotFound:
                    pass  # Az üzenet már törölve lett
            else:
                # Értesítsük a felhasználót, hogy nincs jogosultsága
                try:
                    temp_msg = await message.channel.send(
                        f"{user.mention} Nincs jogosultságod törölni a bot üzeneteit. A 'Snr. Buns' rang szükséges.",
                        delete_after=5  # 5 másodperc után automatikusan törlődik
                    )
                    # Töröljük a felhasználó reakcióját
                    await reaction.remove(user)
                except:
                    pass

@bot.event
async def on_member_join(member):
    """Sends a notification when a new member joins the server."""
    try:
        # Fetch notification channel settings from database
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT notification_channel_id FROM server_settings WHERE guild_id = %s", (member.guild.id,))
        result = cursor.fetchone()
        
        if not result:
            # No notification channel set for this server
            return
            
        notification_channel_id = result[0]
        channel = member.guild.get_channel(notification_channel_id)
        
        if not channel:
            logger.warning(f"Notification channel {notification_channel_id} not found in guild {member.guild.id}")
            return
            
        # Send join notification
        embed = discord.Embed(
            title="Új tag csatlakozott",
            description=f"{member.mention} csatlakozott a szerverhez!",
            color=discord.Color.green()
        )
        embed.add_field(name="Felhasználónév", value=str(member), inline=True)
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Fiók létrehozva", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = datetime.now()
        
        await channel.send(embed=embed)
        logger.info(f"Member join notification sent for {member} (ID: {member.id})")
        
    except Exception as e:
        logger.error(f"Error sending member join notification: {e}")

@bot.event
async def on_member_remove(member):
    """Sends a notification when a member leaves the server."""
    try:
        # Fetch notification channel settings from database
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT notification_channel_id FROM server_settings WHERE guild_id = %s", (member.guild.id,))
        result = cursor.fetchone()
        
        if not result:
            # No notification channel set for this server
            return
            
        notification_channel_id = result[0]
        channel = member.guild.get_channel(notification_channel_id)
        
        if not channel:
            logger.warning(f"Notification channel {notification_channel_id} not found in guild {member.guild.id}")
            return
            
        # Send leave notification
        embed = discord.Embed(
            title="Tag kilépett",
            description=f"**{member}** kilépett a szerverről!",
            color=discord.Color.red()
        )
        embed.add_field(name="Felhasználónév", value=str(member), inline=True)
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Csatlakozott", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Ismeretlen", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = datetime.now()
        
        await channel.send(embed=embed)
        logger.info(f"Member leave notification sent for {member} (ID: {member.id})")
        
    except Exception as e:
        logger.error(f"Error sending member leave notification: {e}")

# Új parancs az utolsó N üzenet törléséhez
@bot.command(name="purge_bot", aliases=PARANCSOK["TOROL_UZENET"])
@check_role()
async def clear_bot_messages(ctx, count: int = 1):
    """Törli a bot legutóbbi N üzenetét ebből a csatornából."""
    global bot_messages
    
    if count < 1:
        await send_tracked_message(ctx, content="A számnak pozitívnak kell lennie.", delete_after=5)
        return
    
    if count > 50:
        await send_tracked_message(ctx, content="Egyszerre maximum 50 üzenetet törölhetsz.", delete_after=5)
        return
    
    # Keressük meg a bot üzeneteit ebben a csatornában
    deleted = 0
    for message in reversed(bot_messages):
        if deleted >= count:
            break
            
        # Ellenőrizzük, hogy az üzenet ebben a csatornában van-e
        if message.channel.id == ctx.channel.id:
            try:
                await message.delete()
                deleted += 1
                # Rövid szünet, hogy ne érjük el a rate limitet
                await asyncio.sleep(0.5)
            except discord.errors.NotFound:
                # Az üzenet már törölve lett, távolítsuk el a listából
                if message in bot_messages:
                    bot_messages.remove(message)
            except Exception as e:
                logger.error(f"Hiba az üzenet törlésekor: {e}")
    
    # Frissítsük a listát, távolítsuk el a törölt üzeneteket
    new_bot_messages = []
    for msg in bot_messages:
        try:
            # Ha az üzenet törlődött, akkor a channel attribútum lekérése hibát dob
            _ = msg.channel
            new_bot_messages.append(msg)
        except:
            # Ha hiba van, akkor az üzenet valószínűleg törölve lett
            pass

    bot_messages = new_bot_messages
    
    # Küldjünk visszajelzést, ami 5 másodperc után eltűnik
    await ctx.send(f"{deleted} üzenet törölve.", delete_after=5)
    
    # Töröljük a parancsot is
    try:
        await ctx.message.delete()
    except:
        pass

# Megerősítés nézet
class ConfirmView(View):
    def __init__(self, bot, ctx, action_type, action_details, callback_func, *callback_args):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        self.action_type = action_type
        self.action_details = action_details
        self.callback_func = callback_func
        self.callback_args = callback_args

    @discord.ui.button(label="Igen", style=ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        await interaction.response.defer()
        await self.callback_func(interaction, *self.callback_args)
        self.stop()

    @discord.ui.button(label="Nem", style=ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        response = await interaction.response.send_message("Művelet megszakítva.", ephemeral=False)
        # Nyomon követjük a választ
        message = await interaction.original_response()
        await track_interaction_response(interaction, message)
        self.stop()

# Fő menü nézet
class FoMenuView(View):
    def __init__(self, bot, ctx):
        super().__init__(timeout=180)
        self.bot = bot
        self.ctx = ctx

    @discord.ui.button(label="Frakciók listázása", style=ButtonStyle.primary, emoji="📋")
    async def lista_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        await interaction.response.defer()
        try:
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.followup.send("Nincsenek frakciók az adatbázisban.")
                await track_interaction_response(interaction, message)
                return

            # Kategorizáljuk a frakciókat lejárati állapot szerint
            ma = date.today()
            hamarosan_lejaro_frakciok = []  # 2 napon belül lejáró
            aktiv_frakciok = []
            lejart_frakciok = []

            for frakcio in frakciok:
                nev, kod, kezdet, lejarat, hozzaado = frakcio
                lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                
                # Csak a dátum részeket hasonlítjuk össze
                if lejarat_datum < ma:
                    lejart_frakciok.append(frakcio)
                elif lejarat_datum == ma or (lejarat_datum - ma).days <= 2:
                    hamarosan_lejaro_frakciok.append(frakcio)
                else:
                    aktiv_frakciok.append(frakcio)

            # Hamarosan lejáró frakciók embed
            if hamarosan_lejaro_frakciok:
                hamarosan_embed = discord.Embed(title="Hamarosan Lejáró Frakciók (2 napon belül)", color=discord.Color.gold())

                # Három oszlopos elrendezés a hamarosan lejáró frakciókhoz
                for i, frakcio in enumerate(hamarosan_lejaro_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    
                    # Csak a dátum részeket hasonlítjuk össze
                    if lejarat_datum == ma:
                        allapot_szoveg = "🟡 MA lejár!"
                    else:
                        hatralevo_napok = (lejarat_datum - ma).days
                        allapot_szoveg = f"🟡 Hamarosan lejár (még {hatralevo_napok} nap)"

                    hamarosan_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: {allapot_szoveg}",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(hamarosan_lejaro_frakciok) - 1:
                        hamarosan_embed.add_field(name="\u200b", value="\u200b", inline=False)

                message = await interaction.followup.send(embed=hamarosan_embed)
                await track_interaction_response(interaction, message)

            # Aktív frakciók embed
            if aktiv_frakciok:
                aktiv_embed = discord.Embed(title="Aktív Frakciók", color=discord.Color.green())

                # Három oszlopos elrendezés az aktív frakciókhoz
                for i, frakcio in enumerate(aktiv_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    hatralevo_napok = (lejarat_datum - ma).days

                    aktiv_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: 🟢 Aktív (még {hatralevo_napok} nap)",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(aktiv_frakciok) - 1:
                        aktiv_embed.add_field(name="\u200b", value="\u200b", inline=False)

                message = await interaction.followup.send(embed=aktiv_embed)
                await track_interaction_response(interaction, message)

            # Lejárt frakciók embed
            if lejart_frakciok:
                lejart_embed = discord.Embed(title="Lejárt Frakciók", color=discord.Color.red())

                # Három oszlopos elrendezés a lejárt frakciókhoz
                for i, frakcio in enumerate(lejart_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    lejart_napok = (ma - lejarat_datum).days

                    lejart_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: 🔴 Lejárt ({lejart_napok} napja)",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(lejart_frakciok) - 1:
                        lejart_embed.add_field(name="\u200b", value="\u200b", inline=False)

                message = await interaction.followup.send(embed=lejart_embed)
                await track_interaction_response(interaction, message)

            # Ha nincs egy kategória sem
            if not hamarosan_lejaro_frakciok and not aktiv_frakciok and not lejart_frakciok:
                message = await interaction.followup.send("Nincsenek frakciók az adatbázisban.")
                await track_interaction_response(interaction, message)

        except Exception as e:
            logger.error(f"Hiba a frakciók listázásakor: {e}")
            error_details = format_error(e)
            message = await interaction.followup.send(f"Hiba történt a frakciók listázásakor.\n{error_details}")
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Új frakció", style=ButtonStyle.success, emoji="➕")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            modal = UjFrakcioModal(self.bot, self.ctx)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"Hiba az új frakció űrlap megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt az új frakció űrlap megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Szerződés meghosszabbítása", style=ButtonStyle.primary, emoji="🔄")
    async def havi_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            # Lekérjük a frakciókat a legördülő menühöz
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.response.send_message("Nincsenek frakciók az adatbázisban.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Legördülő menü a frakciók kiválasztásához
            view = FrakcioValasztoView(self.bot, self.ctx, "frissit")
            message = await interaction.response.send_message("Válaszd ki a meghosszabbítani kívánt frakciót:", view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a szerződés meghosszabbítás menü megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a szerződés meghosszabbítás menü megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Frakció keresése", style=ButtonStyle.primary, emoji="🔍")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            # Lekérjük a frakciókat a legördülő menühöz
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.response.send_message("Nincsenek frakciók az adatbázisban.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Legördülő menü a frakciók kiválasztásához
            view = FrakcioValasztoView(self.bot, self.ctx, "keres")
            message = await interaction.response.send_message("Válaszd ki a keresett frakciót:", view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a keresés menü megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a keresés menü megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Frakció szerkesztése", style=ButtonStyle.primary, emoji="✏️")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            # Lekérjük a frakciókat a legördülő menühöz
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.response.send_message("Nincsenek frakciók az adatbázisban.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Legördülő menü a frakciók kiválasztásához
            view = FrakcioValasztoView(self.bot, self.ctx, "szerkeszt")
            message = await interaction.response.send_message("Válaszd ki a szerkeszteni kívánt frakciót:", view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a szerkesztés menü megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a szerkesztés menü megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Frakció törlése", style=ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            # Lekérjük a frakciókat a legördülő menühöz
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.response.send_message("Nincsenek frakciók az adatbázisban.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Legördülő menü a frakciók kiválasztásához
            view = FrakcioValasztoView(self.bot, self.ctx, "torol")
            message = await interaction.response.send_message("Válaszd ki a törölni kívánt frakciót:", view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a törlés menü megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a törlés menü megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    @discord.ui.button(label="Gyors +1 hét", style=ButtonStyle.secondary, emoji="⏱️")
    async def quick_week_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            # Lekérjük a frakciókat a legördülő menühöz
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                message = await interaction.response.send_message("Nincsenek frakciók az adatbázisban.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Legördülő menü a frakciók kiválasztásához
            view = FrakcioValasztoView(self.bot, self.ctx, "gyors_het")
            message = await interaction.response.send_message("Válaszd ki a frakciót a +1 hét hozzáadásához:", view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a gyors +1 hét menü megnyitásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a gyors +1 hét menü megnyitásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

# Frakció választó nézet
class FrakcioValasztoView(View):
    def __init__(self, bot, ctx, action_type):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        self.action_type = action_type

        # Frakciók lekérése
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT nev FROM frakciok ORDER BY nev")
        frakciok = cursor.fetchall()

        # Legördülő menü létrehozása
        select = Select(
            placeholder="Válassz egy frakciót...",
            options=[discord.SelectOption(label=frakcio[0], value=frakcio[0]) for frakcio in frakciok]
        )

        async def select_callback(interaction):
            # Jogosultság ellenőrzése
            if not has_required_role(interaction.user):
                await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
                return
                
            frakcio_nev = select.values[0]

            if self.action_type == "frissit":
                modal = SzerzodesMeghosszabbitasModal(self.bot, self.ctx, frakcio_nev)
                await interaction.response.send_modal(modal)
            elif self.action_type == "keres":
                await self.search_faction(interaction, frakcio_nev)
            elif self.action_type == "szerkeszt":
                modal = FrakcioSzerkesztesModal(self.bot, self.ctx, frakcio_nev)
                await interaction.response.send_modal(modal)
            elif self.action_type == "torol":
                await self.confirm_delete_faction(interaction, frakcio_nev)
            elif self.action_type == "gyors_het":
                await self.confirm_quick_week(interaction, frakcio_nev)

        select.callback = select_callback
        self.add_item(select)

    async def search_faction(self, interaction, frakcio_nev):
        try:
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok WHERE nev = %s", (frakcio_nev,))
            frakcio = cursor.fetchone()

            if not frakcio:
                message = await interaction.response.send_message(f"A '{frakcio_nev}' nevű frakció nem található.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            nev, kod, kezdet, lejarat, hozzaado = frakcio

            # Lejárati dátum ellenőrzése
            lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
            ma = date.today()
            
            # Csak a dátum részeket hasonlítjuk össze
            if lejarat_datum < ma:
                status = "🔴 Lejárt"
            else:
                if lejarat_datum == ma:
                    status = "🟡 MA lejár!"
                else:
                    hatralevo_napok = (lejarat_datum - ma).days
                    status = f"🟢 Aktív (még {hatralevo_napok} nap)"

            embed = discord.Embed(title=f"Frakció: {nev}", color=discord.Color.blue())
            embed.add_field(name="Kód", value=kod, inline=True)
            embed.add_field(name="Kezdet", value=kezdet, inline=True)
            embed.add_field(name="Lejárat", value=lejarat, inline=True)
            embed.add_field(name="Hozzáadta", value=hozzaado, inline=True)
            embed.add_field(name="Állapot", value=status, inline=True)

            message = await interaction.response.send_message(embed=embed)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a frakció keresésekor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a frakció keresésekor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    async def confirm_delete_faction(self, interaction, frakcio_nev):
        try:
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum FROM frakciok WHERE nev = %s", (frakcio_nev,))
            frakcio = cursor.fetchone()

            if not frakcio:
                message = await interaction.response.send_message(f"A '{frakcio_nev}' nevű frakció nem található.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            nev, kod, kezdet, lejarat = frakcio

            embed = discord.Embed(
                title="Frakció törlése - Megerősítés",
                description=f"Biztosan törölni szeretnéd ezt a frakciót?\n\n**Név:** {nev}\n**Kód:** {kod}\n**Lejárat:** {lejarat}",
                color=discord.Color.red()
            )

            async def delete_faction(interaction, frakcio_nev):
                try:
                    conn, cursor = ensure_connection()
                    execute_query(cursor, "DELETE FROM frakciok WHERE nev = %s", (frakcio_nev,))
                    conn.commit()

                    embed = discord.Embed(
                        title="Frakció törölve",
                        description=f"A '{frakcio_nev}' nevű frakció sikeresen törölve.",
                        color=discord.Color.green()
                    )
                    message = await interaction.followup.send(embed=embed)
                    await track_interaction_response(interaction, message)
                except Exception as e:
                    logger.error(f"Hiba a frakció törlésekor: {e}")
                    error_details = format_error(e)
                    message = await interaction.followup.send(f"Hiba történt a frakció törlésekor.\n{error_details}")
                    await track_interaction_response(interaction, message)

            view = ConfirmView(self.bot, self.ctx, "törlés", f"Frakció: {frakcio_nev}", delete_faction, frakcio_nev)
            message = await interaction.response.send_message(embed=embed, view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a törlés megerősítésekor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a törlés megerősítésekor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

    async def confirm_quick_week(self, interaction, frakcio_nev):
        try:
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev, lejarat_datum FROM frakciok WHERE nev = %s", (frakcio_nev,))
            frakcio = cursor.fetchone()

            if not frakcio:
                message = await interaction.response.send_message(f"A '{frakcio_nev}' nevű frakció nem található.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            nev, lejarat = frakcio
            lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d')
            uj_lejarat = lejarat_datum + timedelta(weeks=1)

            embed = discord.Embed(
                title="Gyors +1 hét - Megerősítés",
                description=f"Biztosan hozzáadsz +1 hetet ehhez a frakcióhoz?\n\n**Név:** {nev}\n**Jelenlegi lejárat:** {lejarat}\n**Új lejárat:** {uj_lejarat.strftime('%Y-%m-%d')}",
                color=discord.Color.gold()
            )

            async def add_quick_week(interaction, frakcio_nev, uj_lejarat):
                try:
                    conn, cursor = ensure_connection()
                    execute_query(cursor, 
                        "UPDATE frakciok SET lejarat_datum = %s, hozzaado_nev = %s WHERE nev = %s",
                        (uj_lejarat.strftime('%Y-%m-%d'), interaction.user.name, frakcio_nev)
                    )
                    conn.commit()

                    embed = discord.Embed(
                        title="Szerződés meghosszabbítva",
                        description=f"A '{frakcio_nev}' frakció szerződése sikeresen meghosszabbítva +1 héttel.\nÚj lejárati dátum: {uj_lejarat.strftime('%Y-%m-%d')}",
                        color=discord.Color.green()
                    )
                    message = await interaction.followup.send(embed=embed)
                    await track_interaction_response(interaction, message)
                except Exception as e:
                    logger.error(f"Hiba a szerződés meghosszabbításakor: {e}")
                    error_details = format_error(e)
                    message = await interaction.followup.send(f"Hiba történt a szerződés meghosszabbításakor.\n{error_details}")
                    await track_interaction_response(interaction, message)

            view = ConfirmView(self.bot, self.ctx, "gyors_het", f"Frakció: {frakcio_nev}", add_quick_week, frakcio_nev, uj_lejarat)
            message = await interaction.response.send_message(embed=embed, view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba a gyors +1 hét megerősítésekor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt a gyors +1 hét megerősítésekor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

# Új frakció modal
class UjFrakcioModal(Modal):
    def __init__(self, bot, ctx):
        super().__init__(title="Új frakció hozzáadása")
        self.bot = bot
        self.ctx = ctx

        # Input mezők
        self.add_item(TextInput(
            label="Frakció neve",
            placeholder="Írd be a frakció nevét",
            required=True,
            min_length=2,
            max_length=100
        ))

        self.add_item(TextInput(
            label="Frakció kódja",
            placeholder="Írd be a frakció kódját",
            required=True,
            min_length=1,
            max_length=100
        ))

        self.add_item(TextInput(
            label="Időtartam (hetek)",
            placeholder="Írd be, hány hétre fizettek",
            required=True,
            default="1",
            min_length=1,
            max_length=2
        ))

    async def on_submit(self, interaction: discord.Interaction):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            nev = self.children[0].value
            kod = self.children[1].value

            try:
                hetek = int(self.children[2].value)
                if hetek <= 0:
                    raise ValueError("A hetek számának pozitívnak kell lennie")
            except ValueError as ve:
                message = await interaction.response.send_message(f"Hiba: A hetek számának pozitív egész számnak kell lennie. Részletek: {ve}", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            kezdet_datum = datetime.now()
            lejarat_datum = kezdet_datum + timedelta(weeks=hetek)
            hozzaado_nev = interaction.user.name

            # Megerősítés kérése
            embed = discord.Embed(
                title="Új frakció hozzáadása - Megerősítés",
                description=f"Biztosan hozzá szeretnéd adni ezt a frakciót?\n\n**Név:** {nev}\n**Kód:** {kod}\n**Időtartam:** {hetek} hét\n**Lejárat:** {lejarat_datum.strftime('%Y-%m-%d')}",
                color=discord.Color.blue()
            )

            async def add_faction(interaction, nev, kod, hetek, kezdet_datum, lejarat_datum, hozzaado_nev):
                try:
                    conn, cursor = ensure_connection()
                    execute_query(cursor,
                        "INSERT INTO frakciok (nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev) VALUES (%s, %s, %s, %s, %s)",
                        (nev, kod, kezdet_datum.strftime('%Y-%m-%d'), lejarat_datum.strftime('%Y-%m-%d'), hozzaado_nev)
                    )
                    conn.commit()

                    embed = discord.Embed(
                        title="Új frakció hozzáadva",
                        description=f"A '{nev}' nevű frakció sikeresen hozzáadva.",
                        color=discord.Color.green()
                    )
                    message = await interaction.followup.send(embed=embed)
                    await track_interaction_response(interaction, message)
                except psycopg2.errors.UniqueViolation:
                    message = await interaction.followup.send(f"Hiba: A '{nev}' nevű frakció már létezik.")
                    await track_interaction_response(interaction, message)
                except Exception as e:
                    logger.error(f"Hiba a frakció hozzáadásakor: {e}")
                    error_details = format_error(e)
                    message = await interaction.followup.send(f"Hiba történt a frakció hozzáadásakor.\n{error_details}")
                    await track_interaction_response(interaction, message)

            view = ConfirmView(
                self.bot, 
                self.ctx, 
                "hozzáadás", 
                f"Frakció: {nev}", 
                add_faction, 
                nev, kod, hetek, kezdet_datum, lejarat_datum, hozzaado_nev
            )
            message = await interaction.response.send_message(embed=embed, view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba az űrlap feldolgozásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt az űrlap feldolgozásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

# Szerződés meghosszabbítás modal
class SzerzodesMeghosszabbitasModal(Modal):
    def __init__(self, bot, ctx, frakcio_nev):
        # Rövidítsük le a címet, hogy 45 karakternél rövidebb legyen
        if len(frakcio_nev) > 20:
            rovid_nev = frakcio_nev[:17] + "..."
        else:
            rovid_nev = frakcio_nev
        super().__init__(title=f"Meghosszabbítás: {rovid_nev}")
        self.bot = bot
        self.ctx = ctx
        self.frakcio_nev = frakcio_nev

        # Input mezők
        self.add_item(TextInput(
            label="Napok száma (0 = nincs)",
            placeholder="Írd be, hány nappal hosszabbítod meg",
            required=True,
            default="0",
            min_length=1,
            max_length=3  # Maximum 999 nap
        ))

        self.add_item(TextInput(
            label="Hetek száma (0 = nincs)",
            placeholder="Írd be, hány héttel hosszabbítod meg",
            required=True,
            default="0",
            min_length=1,
            max_length=2  # Maximum 99 hét
        ))

        self.add_item(TextInput(
            label="Konkrét dátum (üres = nincs)",
            placeholder="ÉÉÉÉ-HH-NN formátumban (pl. 2023-12-31)",
            required=False,
            min_length=0,
            max_length=10
        ))

    async def on_submit(self, interaction: discord.Interaction):
        # Jogosultság ellenőrzése
        if not has_required_role(interaction.user):
            await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
            return
            
        try:
            try:
                napok = int(self.children[0].value)
                if napok < 0:
                    raise ValueError("A napok számának nem negatívnak kell lennie")
            except ValueError as ve:
                message = await interaction.response.send_message(f"Hiba: A napok számának nem negatív egész számnak kell lennie. Részletek: {ve}", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            try:
                hetek = int(self.children[1].value)
                if hetek < 0:
                    raise ValueError("A hetek számának nem negatívnak kell lennie")
            except ValueError as ve:
                message = await interaction.response.send_message(f"Hiba: A hetek számának nem negatív egész számnak kell lennie. Részletek: {ve}", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            konkret_datum_str = self.children[2].value.strip()
            konkret_datum = None

            if konkret_datum_str:
                try:
                    konkret_datum = datetime.strptime(konkret_datum_str, '%Y-%m-%d')
                except ValueError:
                    message = await interaction.response.send_message("Hiba: A dátumnak ÉÉÉÉ-HH-NN formátumban kell lennie (pl. 2023-12-31).", ephemeral=False)
                    message = await interaction.original_response()
                    await track_interaction_response(interaction, message)
                    return

            # Ellenőrizzük, hogy legalább az egyik érték meg van adva
            if napok == 0 and hetek == 0 and not konkret_datum:
                message = await interaction.response.send_message("Hiba: Legalább a napok, hetek számának, vagy a konkrét dátumnak meg kell lennie adva.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Ellenőrizzük, hogy csak az egyik érték van-e megadva
            megadott_ertekek = 0
            if napok > 0: megadott_ertekek += 1
            if hetek > 0: megadott_ertekek += 1
            if konkret_datum: megadott_ertekek += 1

            if megadott_ertekek > 1:
                message = await interaction.response.send_message("Hiba: Csak napokat VAGY heteket VAGY konkrét dátumot adhatsz meg, nem többet. Kérlek, csak az egyiket állítsd be.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            # Jelenlegi adatok lekérése
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT lejarat_datum FROM frakciok WHERE nev = %s", (self.frakcio_nev,))
            result = cursor.fetchone()

            if not result:
                message = await interaction.response.send_message(f"A '{self.frakcio_nev}' nevű frakció nem létezik.", ephemeral=False)
                message = await interaction.original_response()
                await track_interaction_response(interaction, message)
                return

            jelenlegi_lejarat = datetime.strptime(result[0], '%Y-%m-%d')
            ma = datetime.now()
            hozzaado_nev = interaction.user.name

            # Új lejárati dátum kiszámítása
            if konkret_datum:
                uj_lejarat = konkret_datum
                idotartam_szoveg = f"konkrét dátum: {konkret_datum.strftime('%Y-%m-%d')}"
            else:
                # Időtartam kiszámítása
                idotartam = timedelta(days=napok, weeks=hetek)

                if jelenlegi_lejarat > ma:
                    # Ha még nem járt le, akkor a jelenlegi lejárati dátumhoz adjuk hozzá az új időtartamot
                    uj_lejarat = jelenlegi_lejarat + idotartam
                    kezdet_datum = jelenlegi_lejarat - timedelta(weeks=4)  # Becsült kezdet (nem változtatjuk az adatbázisban)
                else:
                    # Ha már lejárt, akkor a mai dátumtól számítjuk
                    kezdet_datum = ma
                    uj_lejarat = ma + idotartam

                # Időtartam szöveg összeállítása
                if napok > 0:
                    idotartam_szoveg = f"{napok} nap"
                else:
                    idotartam_szoveg = f"{hetek} hét"

            # Megerősítés kérése
            embed = discord.Embed(
                title="Szerződés meghosszabbítása - Megerősítés",
                description=f"Biztosan meghosszabbítod ezt a szerződést?\n\n**Név:** {self.frakcio_nev}\n**Időtartam:** {idotartam_szoveg}\n**Jelenlegi lejárat:** {jelenlegi_lejarat.strftime('%Y-%m-%d')}\n**Új lejárat:** {uj_lejarat.strftime('%Y-%m-%d')}",  
                color=discord.Color.blue()
            )

            async def update_faction(interaction, frakcio_nev, uj_lejarat, hozzaado_nev):
                try:
                    conn, cursor = ensure_connection()

                    # Ha már lejárt és nem konkrét dátumot adtunk meg, frissítjük a kezdő dátumot is
                    if jelenlegi_lejarat <= ma and not konkret_datum:
                        execute_query(cursor,
                            "UPDATE frakciok SET kezdet_datum = %s, lejarat_datum = %s, hozzaado_nev = %s WHERE nev = %s",
                            (ma.strftime('%Y-%m-%d'), uj_lejarat.strftime('%Y-%m-%d'), hozzaado_nev, frakcio_nev)
                        )
                    else:
                        # Ha még nem járt le, vagy konkrét dátumot adtunk meg, csak a lejárati dátumot frissítjük
                        execute_query(cursor,
                            "UPDATE frakciok SET lejarat_datum = %s, hozzaado_nev = %s WHERE nev = %s",
                            (uj_lejarat.strftime('%Y-%m-%d'), hozzaado_nev, frakcio_nev)
                        )

                    conn.commit()

                    embed = discord.Embed(
                        title="Szerződés meghosszabbítva",
                        description=f"A '{frakcio_nev}' frakció szerződése sikeresen meghosszabbítva: {uj_lejarat.strftime('%Y-%m-%d')}",
                        color=discord.Color.green()
                    )
                    message = await interaction.followup.send(embed=embed)
                    await track_interaction_response(interaction, message)
                except Exception as e:
                    logger.error(f"Hiba a szerződés meghosszabbításakor: {e}")
                    error_details = format_error(e)
                    message = await interaction.followup.send(f"Hiba történt a szerződés meghosszabbításakor.\n{error_details}")
                    await track_interaction_response(interaction, message)

            view = ConfirmView(
                self.bot, 
                self.ctx, 
                "meghosszabbítás", 
                f"Frakció: {self.frakcio_nev}", 
                update_faction, 
                self.frakcio_nev, uj_lejarat, hozzaado_nev
            )
            message = await interaction.response.send_message(embed=embed, view=view)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)
        except Exception as e:
            logger.error(f"Hiba az űrlap feldolgozásakor: {e}")
            error_details = format_error(e)
            message = await interaction.response.send_message(f"Hiba történt az űrlap feldolgozásakor.\n{error_details}", ephemeral=False)
            message = await interaction.original_response()
            await track_interaction_response(interaction, message)

# Frakció szerkesztés modal
class FrakcioSzerkesztesModal(Modal):
    def __init__(self, bot, ctx, frakcio_nev):
        # Rövidítsük le a címet, hogy 45 karakternél rövidebb legyen
        if len(frakcio_nev) > 25:
            rovid_nev = frakcio_nev[:22] + "..."
        else:
            rovid_nev = frakcio_nev
        super().__init__(title=f"Szerkesztés: {rovid_nev}")
        self.bot = bot
        self.ctx = ctx
        self.frakcio_nev = frakcio_nev

        # Jelenlegi adatok lekérése
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT nev, kod FROM frakciok WHERE nev = %s", (frakcio_nev,))
        result = cursor.fetchone()

        if result:
            self.eredeti_nev, self.eredeti_kod = result
        else:
            self.eredeti_nev = frakcio_nev
            self.eredeti_kod = ""

        # Input mezők
        self.add_item(TextInput(
            label="Frakció neve",
            placeholder="Írd be a frakció új nevét",
            required=True,
            default=self.eredeti_nev,
            min_length=2,
            max_length=100
        ))

        self.add_item(TextInput(
            label="Frakció kódja",
            placeholder="Írd be a frakció új kódját",
            required=True,
            default=self.eredeti_kod,
            min_length=1,
            max_length=100
        ))

    async def on_submit(self, interaction: discord.Interaction):
      # Jogosultság ellenőrzése
      if not has_required_role(interaction.user):
          await interaction.response.send_message("Nincs jogosultságod használni ezt a funkciót. A funkció használatához 'Snr. Buns' rang szükséges.", ephemeral=True)
          return
      
      try:
          uj_nev = self.children[0].value
          uj_kod = self.children[1].value

          # Ha nem változott semmi
          if uj_nev == self.eredeti_nev and uj_kod == self.eredeti_kod:
              message = await interaction.response.send_message("Nem történt változtatás.", ephemeral=False)
              message = await interaction.original_response()
              await track_interaction_response(interaction, message)
              return

          # Megerősítés kérése
          embed = discord.Embed(
              title="Frakció szerkesztése - Megerősítés",
              description=f"Biztosan szeretnéd módosítani ezt a frakciót?\n\n**Eredeti név:** {self.eredeti_nev}\n**Új név:** {uj_nev}\n**Eredeti kód:** {self.eredeti_kod}\n**Új kód:** {uj_kod}",
              color=discord.Color.blue()
          )

          async def edit_faction(interaction, eredeti_nev, uj_nev, uj_kod):
              try:
                  conn, cursor = ensure_connection()
                  execute_query(cursor,
                      "UPDATE frakciok SET nev = %s, kod = %s WHERE nev = %s",
                      (uj_nev, uj_kod, eredeti_nev)
                  )
                  conn.commit()

                  embed = discord.Embed(
                      title="Frakció szerkesztve",
                      description=f"A frakció sikeresen módosítva.\n**Eredeti név:** {eredeti_nev}\n**Új név:** {uj_nev}",
                      color=discord.Color.green()
                  )
                  message = await interaction.followup.send(embed=embed)
                  await track_interaction_response(interaction, message)
              except psycopg2.errors.UniqueViolation:
                  message = await interaction.followup.send(f"Hiba: A '{uj_nev}' nevű frakció már létezik.")
                  await track_interaction_response(interaction, message)
              except Exception as e:
                  logger.error(f"Hiba a frakció szerkesztésekor: {e}")
                  error_details = format_error(e)
                  message = await interaction.followup.send(f"Hiba történt a frakció szerkesztésekor.\n{error_details}")
                  await track_interaction_response(interaction, message)

          view = ConfirmView(
              self.bot, 
              self.ctx, 
              "szerkesztés", 
              f"Frakció: {self.eredeti_nev}", 
              edit_faction, 
              self.eredeti_nev, uj_nev, uj_kod
          )
          message = await interaction.response.send_message(embed=embed, view=view)
          message = await interaction.original_response()
          await track_interaction_response(interaction, message)
      except Exception as e:
          logger.error(f"Hiba az űrlap feldolgozásakor: {e}")
          error_details = format_error(e)
          message = await interaction.response.send_message(f"Hiba történt az űrlap feldolgozásakor.\n{error_details}", ephemeral=False)
          message = await interaction.original_response()
          await track_interaction_response(interaction, message)

# Automatikus napi frissítés
@tasks.loop(time=time(hour=12, minute=0))  # Every day at 14:00
async def napi_frissites():
  # Refresh database connection 5 minutes before scheduled update
  current_time = datetime.now().time()
  if current_time.hour == 13 and current_time.minute == 55:
      logger.info("Frissítjük az adatbázis kapcsolatot az ütemezett frissítés előtt...")
      try:
          conn, cursor = ensure_connection()
          logger.info("Adatbázis kapcsolat sikeresen frissítve.")
      except Exception as e:
          logger.error(f"Hiba az adatbázis kapcsolat frissítésekor: {e}")
  
  logger.info("Napi frissítés elindítva...")
  
  # Minden szerverre végigmegyünk, ahol a bot jelen van
  for guild in bot.guilds:
      # Adatbázisból lekérjük az automatikus frissítés beállításait
      conn, cursor = ensure_connection()
      execute_query(cursor, "SELECT csatorna_id, cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id FROM auto_frissites WHERE aktiv = TRUE")
      auto_frissites_lista = cursor.fetchall()
      
      for beallitas in auto_frissites_lista:
          csatorna_id, cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id = beallitas
          
          # Megpróbáljuk lekérni a csatornát
          csatorna = bot.get_channel(csatorna_id)
          if csatorna is None:
              logger.warning(f"A(z) {csatorna_id} azonosítójú csatorna nem található. Kikapcsoljuk az automatikus frissítést.")
              execute_query(cursor, "UPDATE auto_frissites SET aktiv = FALSE WHERE csatorna_id = %s", (csatorna_id,))
              conn.commit()
              continue
          
          try:
              # Előző üzenetek törlése (ha léteznek)
              üzenet_idk = [cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id]
              for üzenet_id in üzenet_idk:
                  if üzenet_id:
                      try:
                          üzenet = await csatorna.fetch_message(üzenet_id)
                          await üzenet.delete()
                          logger.info(f"Előző üzenet ({üzenet_id}) törölve a {csatorna.name} csatornán.")
                      except discord.NotFound:
                          logger.info(f"Előző üzenet ({üzenet_id}) már nem létezik a {csatorna.name} csatornán.")
                      except Exception as e:
                          logger.error(f"Hiba az előző üzenet ({üzenet_id}) törlésekor: {e}")
              
              # Adatbázisban töröljük a korábbi üzenet azonosítókat
              execute_query(cursor, """
                  UPDATE auto_frissites 
                  SET cim_uzenet_id = NULL, hamarosan_uzenet_id = NULL, 
                  aktiv_uzenet_id = NULL, lejart_uzenet_id = NULL 
                  WHERE csatorna_id = %s
              """, (csatorna_id,))
              conn.commit()

              # Frakciók lekérése az adatbázisból
              conn, cursor = ensure_connection()
              execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok ORDER BY nev")
              frakciok = cursor.fetchall()

              if not frakciok:
                  logger.info(f"Nincsenek frakciók az adatbázisban a(z) {csatorna.name} csatornán.")
                  continue

              # Címüzenet létrehozása (mindig új)
              cim_embed = discord.Embed(
                  title="ÚJ NAPI FRAKCIÓ FRISSÍTÉS ÉRKEZETT!",
                  description=f"📅 Napi frakció lista - {datetime.now().strftime('%Y-%m-%d')}\nAutomatikus napi frissítés\nUtolsó frissítés: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                  color=discord.Color.blue()
              )
              
              # Új címüzenetet küldünk
              uj_cim_uzenet = await csatorna.send(embed=cim_embed)
              execute_query(cursor, "UPDATE auto_frissites SET cim_uzenet_id = %s WHERE csatorna_id = %s", (uj_cim_uzenet.id, csatorna_id))
              conn.commit()
              
              # Kategorizáljuk a frakciókat lejárati állapot szerint
              ma = date.today()
              hamarosan_lejaro_frakciok = []  # 2 napon belül lejáró
              aktiv_frakciok = []
              lejart_frakciok = []

              for frakcio in frakciok:
                  nev, kod, kezdet, lejarat, hozzaado = frakcio
                  lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                  
                  # Csak a dátum részeket hasonlítjuk össze
                  if lejarat_datum < ma:
                      lejart_frakciok.append(frakcio)
                  elif lejarat_datum == ma or (lejarat_datum - ma).days <= 2:
                      hamarosan_lejaro_frakciok.append(frakcio)
                  else:
                      aktiv_frakciok.append(frakcio)

              # Az egyes frakció-kategóriák megjelenítése...
              # Hamarosan lejáró frakciók embed
              if hamarosan_lejaro_frakciok:
                  hamarosan_embed = discord.Embed(title="Hamarosan Lejáró Frakciók (2 napon belül)", color=discord.Color.gold())

                  # Három oszlopos elrendezés a hamarosan lejáró frakciókhoz
                  for i, frakcio in enumerate(hamarosan_lejaro_frakciok):
                      nev, kod, kezdet, lejarat, hozzaado = frakcio
                      lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                      
                      # Csak a dátum részeket hasonlítjuk össze
                      if lejarat_datum == ma:
                          allapot_szoveg = "🟡 MA lejár!"
                      else:
                          hatralevo_napok = (lejarat_datum - ma).days
                          allapot_szoveg = f"🟡 Hamarosan lejár (még {hatralevo_napok} nap)"

                      hamarosan_embed.add_field(
                          name=f"{nev} ({kod})",
                          value=f"Lejárat: {lejarat}\nÁllapot: {allapot_szoveg}",
                          inline=True
                      )

                      # Minden harmadik után üres mező a sorváltáshoz
                      if (i + 1) % 3 == 0 and i < len(hamarosan_lejaro_frakciok) - 1:
                          hamarosan_embed.add_field(name="\u200b", value="\u200b", inline=False)

                  # Új üzenetet küldünk
                  uj_hamarosan_uzenet = await csatorna.send(embed=hamarosan_embed)
                  execute_query(cursor, "UPDATE auto_frissites SET hamarosan_uzenet_id = %s WHERE csatorna_id = %s", 
                              (uj_hamarosan_uzenet.id, csatorna_id))
                  conn.commit()

              # Aktív frakciók embed
              if aktiv_frakciok:
                  aktiv_embed = discord.Embed(title="Aktív Frakciók", color=discord.Color.green())

                  # Három oszlopos elrendezés az aktív frakciókhoz
                  for i, frakcio in enumerate(aktiv_frakciok):
                      nev, kod, kezdet, lejarat, hozzaado = frakcio
                      lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                      hatralevo_napok = (lejarat_datum - ma).days

                      aktiv_embed.add_field(
                          name=f"{nev} ({kod})",
                          value=f"Lejárat: {lejarat}\nÁllapot: 🟢 Aktív (még {hatralevo_napok} nap)",
                          inline=True
                      )

                      # Minden harmadik után üres mező a sorváltáshoz
                      if (i + 1) % 3 == 0 and i < len(aktiv_frakciok) - 1:
                          aktiv_embed.add_field(name="\u200b", value="\u200b", inline=False)

                  # Új üzenetet küldünk
                  uj_aktiv_uzenet = await csatorna.send(embed=aktiv_embed)
                  execute_query(cursor, "UPDATE auto_frissites SET aktiv_uzenet_id = %s WHERE csatorna_id = %s", 
                              (uj_aktiv_uzenet.id, csatorna_id))
                  conn.commit()

              # Lejárt frakciók embed
              if lejart_frakciok:
                  lejart_embed = discord.Embed(title="Lejárt Frakciók", color=discord.Color.red())

                  # Három oszlopos elrendezés a lejárt frakciókhoz
                  for i, frakcio in enumerate(lejart_frakciok):
                      nev, kod, kezdet, lejarat, hozzaado = frakcio
                      lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                      lejart_napok = (ma - lejarat_datum).days

                      lejart_embed.add_field(
                          name=f"{nev} ({kod})",
                          value=f"Lejárat: {lejarat}\nÁllapot: 🔴 Lejárt ({lejart_napok} napja)",
                          inline=True
                      )

                      # Minden harmadik után üres mező a sorváltáshoz
                      if (i + 1) % 3 == 0 and i < len(lejart_frakciok) - 1:
                          lejart_embed.add_field(name="\u200b", value="\u200b", inline=False)

                  # Új üzenetet küldünk
                  uj_lejart_uzenet = await csatorna.send(embed=lejart_embed)
                  execute_query(cursor, "UPDATE auto_frissites SET lejart_uzenet_id = %s WHERE csatorna_id = %s", 
                              (uj_lejart_uzenet.id, csatorna_id))
                  conn.commit()
                      
              # Utolsó frissítés időpontjának beállítása
              most = datetime.now()
              execute_query(cursor, "UPDATE auto_frissites SET utolso_frissites = %s WHERE csatorna_id = %s", (most.strftime('%Y-%m-%d %H:%M:%S'), csatorna_id))
              conn.commit()
              logger.info(f"A(z) {csatorna.name} csatorna automatikus frissítése sikeresen befejeződött.")

          except Exception as e:
              logger.error(f"Hiba a napi frissítés során a(z) {csatorna.name} csatornán: {e}")
  
  logger.info("Napi frissítés befejezve.")

# Automatikus napi frissítés - teszt verzió
@tasks.loop(seconds=60)  # Például 60 másodpercenként
async def napi_frissites_teszt():
    logger.info("TESZT: Napi frissítés elindítva...")
    
    # Minden szerverre végigmegyünk, ahol a bot jelen van
    for guild in bot.guilds:
        # Adatbázisból lekérjük az automatikus frissítés beállításait
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT csatorna_id, cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id FROM auto_frissites WHERE aktiv = TRUE")
        auto_frissites_lista = cursor.fetchall()
        
        for beallitas in auto_frissites_lista:
            csatorna_id, cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id = beallitas
            
            # Megpróbáljuk lekérni a csatornát
            csatorna = bot.get_channel(csatorna_id)
            if csatorna is None:
                logger.warning(f"A(z) {csatorna_id} azonosítójú csatorna nem található. Kikapcsoljuk az automatikus frissítést.")
                execute_query(cursor, "UPDATE auto_frissites SET aktiv = FALSE WHERE csatorna_id = %s", (csatorna_id,))
                conn.commit()
                continue
            
            try:
                # Előző üzenetek törlése (ha léteznek)
                üzenet_idk = [cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id]
                for üzenet_id in üzenet_idk:
                    if üzenet_id:
                        try:
                            üzenet = await csatorna.fetch_message(üzenet_id)
                            await uzenet.delete()
                            logger.info(f"Előző üzenet ({üzenet_id}) törölve a {csatorna.name} csatornán.")
                        except discord.NotFound:
                            logger.info(f"Előző üzenet ({üzenet_id}) már nem létezik a {csatorna.name} csatornán.")
                        except Exception as e:
                            logger.error(f"Hiba az előző üzenet ({üzenet_id}) törlésekor: {e}")
                
                # Adatbázisban töröljük a korábbi üzenet azonosítókat
                execute_query(cursor, """
                    UPDATE auto_frissites 
                    SET cim_uzenet_id = NULL, hamarosan_uzenet_id = NULL, 
                    aktiv_uzenet_id = NULL, lejart_uzenet_id = NULL 
                    WHERE csatorna_id = %s
                """, (csatorna_id,))
                conn.commit()

                # Frakciók lekérése az adatbázisból
                conn, cursor = ensure_connection()
                execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok ORDER BY nev")
                frakciok = cursor.fetchall()

                if not frakciok:
                    logger.info(f"Nincsenek frakciók az adatbázisban a(z) {csatorna.name} csatornán.")
                    continue

                # Címüzenet létrehozása (mindig új)
                cim_embed = discord.Embed(
                    title="ÚJ NAPI FRAKCIÓ FRISSÍTÉS ÉRKEZETT!",
                    description=f"📅 Napi frakció lista - {datetime.now().strftime('%Y-%m-%d')}\nAutomatikus napi frissítés\nUtolsó frissítés: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    color=discord.Color.blue()
                )
                
                # Új címüzenetet küldünk
                uj_cim_uzenet = await csatorna.send(embed=cim_embed)
                execute_query(cursor, "UPDATE auto_frissites SET cim_uzenet_id = %s WHERE csatorna_id = %s", (uj_cim_uzenet.id, csatorna_id))
                conn.commit()

                # Kategorizáljuk a frakciókat lejárati állapot szerint
                ma = date.today()
                hamarosan_lejaro_frakciok = []  # 2 napon belül lejáró
                aktiv_frakciok = []
                lejart_frakciok = []

                for frakcio in frakciok:
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    
                    # Csak a dátum részeket hasonlítjuk össze
                    if lejarat_datum < ma:
                        lejart_frakciok.append(frakcio)
                    elif lejarat_datum == ma or (lejarat_datum - ma).days <= 2:
                        hamarosan_lejaro_frakciok.append(frakcio)
                    else:
                        aktiv_frakciok.append(frakcio)

                # Hamarosan lejáró frakciók embed
                if hamarosan_lejaro_frakciok:
                    hamarosan_embed = discord.Embed(title="Hamarosan Lejáró Frakciók (2 napon belül)", color=discord.Color.gold())

                    # Három oszlopos elrendezés a hamarosan lejáró frakciókhoz
                    for i, frakcio in enumerate(hamarosan_lejaro_frakciok):
                        nev, kod, kezdet, lejarat, hozzaado = frakcio
                        lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                        
                        # Csak a dátum részeket hasonlítjuk össze
                        if lejarat_datum == ma:
                            allapot_szoveg = "🟡 MA lejár!"
                        else:
                            hatralevo_napok = (lejarat_datum - ma).days
                            allapot_szoveg = f"🟡 Hamarosan lejár (még {hatralevo_napok} nap)"

                        hamarosan_embed.add_field(
                            name=f"{nev} ({kod})",
                            value=f"Lejárat: {lejarat}\nÁllapot: {allapot_szoveg}",
                            inline=True
                        )

                        # Minden harmadik után üres mező a sorváltáshoz
                        if (i + 1) % 3 == 0 and i < len(hamarosan_lejaro_frakciok) - 1:
                            hamarosan_embed.add_field(name="\u200b", value="\u200b", inline=False)

                    # Új üzenetet küldünk
                    uj_hamarosan_uzenet = await csatorna.send(embed=hamarosan_embed)
                    execute_query(cursor, "UPDATE auto_frissites SET hamarosan_uzenet_id = %s WHERE csatorna_id = %s", 
                                (uj_hamarosan_uzenet.id, csatorna_id))
                    conn.commit()

                # Aktív frakciók embed
                if aktiv_frakciok:
                    aktiv_embed = discord.Embed(title="Aktív Frakciók", color=discord.Color.green())

                    # Három oszlopos elrendezés az aktív frakciókhoz
                    for i, frakcio in enumerate(aktiv_frakciok):
                        nev, kod, kezdet, lejarat, hozzaado = frakcio
                        lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                        hatralevo_napok = (lejarat_datum - ma).days

                        aktiv_embed.add_field(
                            name=f"{nev} ({kod})",
                            value=f"Lejárat: {lejarat}\nÁllapot: 🟢 Aktív (még {hatralevo_napok} nap)",
                            inline=True
                        )

                        # Minden harmadik után üres mező a sorváltáshoz
                        if (i + 1) % 3 == 0 and i < len(aktiv_frakciok) - 1:
                            aktiv_embed.add_field(name="\u200b", value="\u200b", inline=False)

                    # Új üzenetet küldünk
                    uj_aktiv_uzenet = await csatorna.send(embed=aktiv_embed)
                    execute_query(cursor, "UPDATE auto_frissites SET aktiv_uzenet_id = %s WHERE csatorna_id = %s", 
                                (uj_aktiv_uzenet.id, csatorna_id))
                    conn.commit()

                # Lejárt frakciók embed
                if lejart_frakciok:
                    lejart_embed = discord.Embed(title="Lejárt Frakciók", color=discord.Color.red())

                    # Három oszlopos elrendezés a lejárt frakciókhoz
                    for i, frakcio in enumerate(lejart_frakciok):
                        nev, kod, kezdet, lejarat, hozzaado = frakcio
                        lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                        lejart_napok = (ma - lejarat_datum).days

                        lejart_embed.add_field(
                            name=f"{nev} ({kod})",
                            value=f"Lejárat: {lejarat}\nÁllapot: 🔴 Lejárt ({lejart_napok} napja)",
                            inline=True
                        )

                        # Minden harmadik után üres mező a sorváltáshoz
                        if (i + 1) % 3 == 0 and i < len(lejart_frakciok) - 1:
                            lejart_embed.add_field(name="\u200b", value="\u200b", inline=False)

                    # Új üzenetet küldünk
                    uj_lejart_uzenet = await csatorna.send(embed=lejart_embed)
                    execute_query(cursor, "UPDATE auto_frissites SET lejart_uzenet_id = %s WHERE csatorna_id = %s", 
                                (uj_lejart_uzenet.id, csatorna_id))
                    conn.commit()
                        
                # Utolsó frissítés időpontjának beállítása
                most = datetime.now()
                execute_query(cursor, "UPDATE auto_frissites SET utolso_frissites = %s WHERE csatorna_id = %s", (most.strftime('%Y-%m-%d %H:%M:%S'), csatorna_id))
                conn.commit()
                logger.info(f"A(z) {csatorna.name} csatorna automatikus frissítése sikeresen befejeződött.")

            except Exception as e:
                logger.error(f"Hiba a napi frissítés során a(z) {csatorna.name} csatornán: {e}")
    
    logger.info("TESZT: Napi frissítés befejezve.")

# A napi_frissites_teszt függvény után, de a teszt_frissites parancs előtt adjuk hozzá az új parancsot:

@bot.command(name="beallitas_ertesites_csatorna")
@check_role()
async def set_notification_channel(ctx, channel: discord.TextChannel = None):
    """Beállítja a csatlakozási/kilépési értesítések csatornáját."""
    try:
        # Ha nincs megadva csatorna, akkor az aktuális csatornát használjuk
        if csatorna is None:
            csatorna = ctx.channel
        
        conn, cursor = ensure_connection()
        
        # Ellenőrizzük, hogy van-e már beállítás ehhez a szerverhez
        execute_query(cursor, "SELECT notification_channel_id FROM server_settings WHERE guild_id = %s", (ctx.guild.id,))
        result = cursor.fetchone()
        
        if result:
            # Frissítjük a meglévő beállítást
            execute_query(cursor, 
                "UPDATE server_settings SET notification_channel_id = %s WHERE guild_id = %s",
                (channel.id, ctx.guild.id)
            )
        else:
            # Új beállítást hozunk létre
            execute_query(cursor, 
                "INSERT INTO server_settings (guild_id, notification_channel_id) VALUES (%s, %s)",
                (ctx.guild.id, channel.id)
            )
        
        conn.commit()
        
        embed = discord.Embed(
            title="Értesítési csatorna beállítva",
            description=f"A csatlakozási és kilépési értesítések a következő csatornára lesznek küldve: {channel.mention}",
            color=discord.Color.green()
        )
        
        await send_tracked_message(ctx, embed=embed)
        logger.info(f"Notification channel set to {channel.name} (ID: {channel.id}) for guild {ctx.guild.name} (ID: {ctx.guild.id})")
        
    except Exception as e:
        logger.error(f"Error setting notification channel: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt az értesítési csatorna beállításakor.\n{error_details}")

# A napi_frissites_teszt függvény után, de a teszt_frissites parancs előtt adjuk hozzá az új parancsot:

@bot.command(name="auto_frissites_beallitas", aliases=["auto_lista", "auto_update"])
@check_role()
async def auto_frissites_beallitas(ctx):
    """Automatikus napi frissítés beállítása az aktuális csatornán."""
    try:
        # Mindig az aktuális csatornát használjuk
        csatorna = ctx.channel
        
        # Ellenőrizzük, hogy van-e már beállítás erre a csatornára
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT aktiv FROM auto_frissites WHERE csatorna_id = %s", (csatorna.id,))
        result = cursor.fetchone()
        
        if result:
            # Ha már létezik beállítás, ellenőrizzük, hogy aktív-e
            if result[0] if not is_sqlite else bool(result[0]):
                await send_tracked_message(ctx, content=f"Az automatikus frissítés már be van állítva ezen a csatornán.")
                return
            else:
                # Ha nem aktív, akkor aktiváljuk újra
                execute_query(cursor, "UPDATE auto_frissites SET aktiv = TRUE WHERE csatorna_id = %s", (csatorna.id,))
                conn.commit()
                await send_tracked_message(ctx, content=f"Az automatikus frissítés újra aktiválva ezen a csatornán.")
                return
        
        # Ha még nincs beállítás, akkor létrehozzuk
        execute_query(cursor, 
            "INSERT INTO auto_frissites (csatorna_id, aktiv) VALUES (%s, TRUE)",
            (csatorna.id,)
        )
        conn.commit()
        
        await send_tracked_message(ctx, content=f"Az automatikus frissítés sikeresen beállítva ezen a csatornán. A frissítés minden nap 14:00-kor fog megtörténni.")
        logger.info(f"Automatikus frissítés beállítva a(z) {csatorna.name} csatornán.")
        
    except Exception as e:
        logger.error(f"Hiba az automatikus frissítés beállításakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt az automatikus frissítés beállításakor.\n{error_details}")

@bot.command(name="auto_frissites_kikapcsolas", aliases=["auto_off", "auto_disable", "automatikus_frissites_kikapcsolasa"])
@check_role()
async def auto_frissites_kikapcsolas(ctx, csatorna: discord.TextChannel = None):
    """Automatikus napi frissítés kikapcsolása egy csatornán."""
    try:
        # Ha nincs megadva csatorna, akkor az aktuális csatornát használjuk
        if csatorna is None:
            csatorna = ctx.channel
        
        # Ellenőrizzük, hogy van-e beállítás erre a csatornára
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT aktiv FROM auto_frissites WHERE csatorna_id = %s", (csatorna.id,))
        result = cursor.fetchone()
        
        if not result:
            await send_tracked_message(ctx, content=f"Az automatikus frissítés nincs beállítva a {csatorna.mention} csatornán.")
            return
        
        # Ha már nem aktív, akkor jelezzük
        if not (result[0] if not is_sqlite else bool(result[0])):
            await send_tracked_message(ctx, content=f"Az automatikus frissítés már ki van kapcsolva a {csatorna.mention} csatornán.")
            return
        
        # Kikapcsoljuk az automatikus frissítést
        execute_query(cursor, "UPDATE auto_frissites SET aktiv = FALSE WHERE csatorna_id = %s", (csatorna.id,))
        conn.commit()
        
        await send_tracked_message(ctx, content=f"Az automatikus frissítés sikeresen kikapcsolva a {csatorna.mention} csatornán.")
        logger.info(f"Automatikus frissítés kikapcsolva a(z) {csatorna.name} csatornán.")
        
    except Exception as e:
        logger.error(f"Hiba az automatikus frissítés kikapcsolásakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt az automatikus frissítés kikapcsolásakor.\n{error_details}")

# Parancs az automatikus frissítés teszteléséhez
@bot.command(name="teszt_frissites", aliases=PARANCSOK["TESZT_FRISSITES"])
@check_role()
async def teszt_frissites(ctx):
    """Elindít egy teszt frissítést az aktuális csatornán."""
    try:
        await send_tracked_message(ctx, content="A teszt frissítés elindult. Kérlek, várj...")
        logger.info(f"Teszt frissítés elindítva a(z) {ctx.channel.name} csatornán.")
        
        # Közvetlenül frissítjük a frakció listát az aktuális csatornán
        csatorna = ctx.channel
        
        try:
            # Ellenőrizzük, hogy van-e beállítás ehhez a csatornához, és ha igen, töröljük a korábbi üzeneteket
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id FROM auto_frissites WHERE csatorna_id = %s", (csatorna.id,))
            result = cursor.fetchone()
            
            if result:
                # Ha van beállítás, töröljük a korábbi üzeneteket
                cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id = result
                üzenet_idk = [cim_uzenet_id, hamarosan_uzenet_id, aktiv_uzenet_id, lejart_uzenet_id]
                
                for üzenet_id in üzenet_idk:
                    if üzenet_id:
                        try:
                            üzenet = await csatorna.fetch_message(üzenet_id)
                            await uzenet.delete()
                            logger.info(f"Előző üzenet ({üzenet_id}) törölve a {csatorna.name} teszt csatornán.")
                        except discord.NotFound:
                            logger.info(f"Előző üzenet ({üzenet_id}) már nem létezik a {csatorna.name} teszt csatornán.")
                        except Exception as e:
                            logger.error(f"Hiba az előző üzenet ({üzenet_id}) törlésekor a teszt során: {e}")
                
                # Adatbázisban töröljük a korábbi üzenet azonosítókat
                execute_query(cursor, """
                    UPDATE auto_frissites 
                    SET cim_uzenet_id = NULL, hamarosan_uzenet_id = NULL, 
                    aktiv_uzenet_id = NULL, lejart_uzenet_id = NULL 
                    WHERE csatorna_id = %s
                """, (csatorna.id,))
                conn.commit()
            
            # Frakciók lekérése az adatbázisból
            conn, cursor = ensure_connection()
            execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok ORDER BY nev")
            frakciok = cursor.fetchall()

            if not frakciok:
                await send_tracked_message(ctx, content="Nincsenek frakciók az adatbázisban.")
                return

            # Címüzenet létrehozása és küldése
            cim_embed = discord.Embed(
                title="ÚJ NAPI FRAKCIÓ FRISSÍTÉS ÉRKEZETT!",
                description=f"📅 Napi frakció lista - {datetime.now().strftime('%Y-%m-%d')}\nAutomatikus napi frissítés\nUtolsó frissítés: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                color=discord.Color.blue()
            )
            cim_uzenet = await send_tracked_message(ctx, embed=cim_embed)
            
            # Ha van beállítás, frissítjük a címüzenet azonosítóját
            if result:
                execute_query(cursor, "UPDATE auto_frissites SET cim_uzenet_id = %s WHERE csatorna_id = %s", (cim_uzenet.id, csatorna.id))
                conn.commit()
            
            # Kategorizáljuk a frakciókat lejárati állapot szerint
            ma = date.today()
            hamarosan_lejaro_frakciok = []  # 2 napon belül lejáró
            aktiv_frakciok = []
            lejart_frakciok = []

            for frakcio in frakciok:
                nev, kod, kezdet, lejarat, hozzaado = frakcio
                lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                
                # Csak a dátum részeket hasonlítjuk össze
                if lejarat_datum < ma:
                    lejart_frakciok.append(frakcio)
                elif lejarat_datum == ma or (lejarat_datum - ma).days <= 2:
                    hamarosan_lejaro_frakciok.append(frakcio)
                else:
                    aktiv_frakciok.append(frakcio)

            # Hamarosan lejáró frakciók embed
            if hamarosan_lejaro_frakciok:
                hamarosan_embed = discord.Embed(title="Hamarosan Lejáró Frakciók (2 napon belül)", color=discord.Color.gold())

                # Három oszlopos elrendezés a hamarosan lejáró frakciókhoz
                for i, frakcio in enumerate(hamarosan_lejaro_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    
                    # Csak a dátum részeket hasonlítjuk össze
                    if lejarat_datum == ma:
                        allapot_szoveg = "🟡 MA lejár!"
                    else:
                        hatralevo_napok = (lejarat_datum - ma).days
                        allapot_szoveg = f"🟡 Hamarosan lejár (még {hatralevo_napok} nap)"

                    hamarosan_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: {allapot_szoveg}",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(hamarosan_lejaro_frakciok) - 1:
                        hamarosan_embed.add_field(name="\u200b", value="\u200b", inline=False)

                hamarosan_uzenet = await send_tracked_message(ctx, embed=hamarosan_embed)
                
                # Ha van beállítás, frissítjük a hamarosan üzenet azonosítóját
                if result:
                    execute_query(cursor, "UPDATE auto_frissites SET hamarosan_uzenet_id = %s WHERE csatorna_id = %s", (hamarosan_uzenet.id, csatorna.id))
                    conn.commit()

            # Aktív frakciók embed
            if aktiv_frakciok:
                aktiv_embed = discord.Embed(title="Aktív Frakciók", color=discord.Color.green())

                # Három oszlopos elrendezés az aktív frakciókhoz
                for i, frakcio in enumerate(aktiv_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    hatralevo_napok = (lejarat_datum - ma).days

                    aktiv_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: 🟢 Aktív (még {hatralevo_napok} nap)",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(aktiv_frakciok) - 1:
                        aktiv_embed.add_field(name="\u200b", value="\u200b", inline=False)

                aktiv_uzenet = await send_tracked_message(ctx, embed=aktiv_embed)
                
                # Ha van beállítás, frissítjük az aktív üzenet azonosítóját
                if result:
                    execute_query(cursor, "UPDATE auto_frissites SET aktiv_uzenet_id = %s WHERE csatorna_id = %s", (aktiv_uzenet.id, csatorna.id))
                    conn.commit()

            # Lejárt frakciók embed
            if lejart_frakciok:
                lejart_embed = discord.Embed(title="Lejárt Frakciók", color=discord.Color.red())

                # Három oszlopos elrendezés a lejárt frakciókhoz
                for i, frakcio in enumerate(lejart_frakciok):
                    nev, kod, kezdet, lejarat, hozzaado = frakcio
                    lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                    lejart_napok = (ma - lejarat_datum).days

                    lejart_embed.add_field(
                        name=f"{nev} ({kod})",
                        value=f"Lejárat: {lejarat}\nÁllapot: 🔴 Lejárt ({lejart_napok} napja)",
                        inline=True
                    )

                    # Minden harmadik után üres mező a sorváltáshoz
                    if (i + 1) % 3 == 0 and i < len(lejart_frakciok) - 1:
                        lejart_embed.add_field(name="\u200b", value="\u200b", inline=False)

                lejart_uzenet = await send_tracked_message(ctx, embed=lejart_embed)
                
                # Ha van beállítás, frissítjük a lejárt üzenet azonosítóját
                if result:
                    execute_query(cursor, "UPDATE auto_frissites SET lejart_uzenet_id = %s WHERE csatorna_id = %s", (lejart_uzenet.id, csatorna.id))
                    conn.commit()

            # Ha nincs egy kategória sem
            if not hamarosan_lejaro_frakciok and not aktiv_frakciok and not lejart_frakciok:
                await send_tracked_message(ctx, content="Nincsenek frakciók az adatbázisban.")
                
            # Frissítjük az utolsó frissítés időpontját az adatbázisban
            if result:
                most = datetime.now()
                execute_query(cursor, "UPDATE auto_frissites SET utolso_frissites = %s WHERE csatorna_id = %s", (most.strftime('%Y-%m-%d %H:%M:%S'), csatorna.id))
                conn.commit()
                
            await send_tracked_message(ctx, content="A teszt frissítés sikeresen befejeződött.")
            logger.info(f"Teszt frissítés sikeresen befejeződött a(z) {ctx.channel.name} csatornán.")

        except Exception as e:
            logger.error(f"Hiba a teszt frissítés során a(z) {ctx.channel.name} csatornán: {e}")
            error_details = format_error(e)
            await send_tracked_message(ctx, content=f"Hiba történt a teszt frissítés során.\n{error_details}")
            
    except Exception as e:
        logger.error(f"Hiba a teszt frissítés indításakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a teszt frissítés indításakor.\n{error_details}")

# Parancs az automatikus frissítés teszteléséhez
@bot.command(name="auto_teszt_inditas", aliases=PARANCSOK["AUTO_TEST"])
@check_role()
async def auto_teszt_inditas(ctx):
    """Elindít egy automatikus frissítést az aktuális csatornán."""
    try:
        # Ellenőrizzük, hogy fut-e már a frissítés
        if napi_frissites.is_running():
            # Leállítjuk a futó feladatot
            napi_frissites.cancel()
            await send_tracked_message(ctx, content="A futó automatikus frissítés leállítva. Új frissítés indítása...")
            await asyncio.sleep(1)  # Várunk egy kicsit, hogy biztosan leálljon
        
        # Indítjuk a frissítést
        napi_frissites.start()
        await send_tracked_message(ctx, content="Az automatikus frissítés elindult. Kérlek, várj...")
        logger.info("Automatikus frissítés elindítva.")
    except Exception as e:
        logger.error(f"Hiba az automatikus frissítés indításakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt az automatikus frissítés indításakor.\n{error_details}")

# Főmenü parancs
@bot.command(name="menu", aliases=PARANCSOK["MENU"])
@check_role()
async def menu(ctx):
    """Megnyitja a főmenüt."""
    view = FoMenuView(bot, ctx)
    embed = discord.Embed(title="Főmenü", description="Válassz egy opciót:", color=discord.Color.blue())
    message = await send_tracked_message(ctx, embed=embed, view=view)

# Szöveges parancsok
@bot.command(name="uj_frakcio")
@check_role()
async def uj_frakcio(ctx):
    """Megnyitja az új frakció hozzáadása űrlapot."""
    try:
        modal = UjFrakcioModal(bot, ctx)
        await ctx.send_modal(modal)
    except Exception as e:
        logger.error(f"Hiba az új frakció űrlap megnyitásakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt az új frakció űrlap megnyitásakor.\n{error_details}")

@bot.command(name="lista", aliases=PARANCSOK["LISTA"])
@check_role()
async def lista(ctx):
    """Listázza az összes frakciót."""
    try:
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok ORDER BY nev")
        frakciok = cursor.fetchall()

        if not frakciok:
            await send_tracked_message(ctx, content="Nincsenek frakciók az adatbázisban.")
            return

        # Kategorizáljuk a frakciókat lejárati állapot szerint
        ma = date.today()
        hamarosan_lejaro_frakciok = []  # 2 napon belül lejáró
        aktiv_frakciok = []
        lejart_frakciok = []

        for frakcio in frakciok:
            nev, kod, kezdet, lejarat, hozzaado = frakcio
            lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
            
            # Csak a dátum részeket hasonlítjuk össze
            if lejarat_datum < ma:
                lejart_frakciok.append(frakcio)
            elif lejarat_datum == ma or (lejarat_datum - ma).days <= 2:
                hamarosan_lejaro_frakciok.append(frakcio)
            else:
                aktiv_frakciok.append(frakcio)

        # Hamarosan lejáró frakciók embed
        if hamarosan_lejaro_frakciok:
            hamarosan_embed = discord.Embed(title="Hamarosan Lejáró Frakciók (2 napon belül)", color=discord.Color.gold())

            # Három oszlopos elrendezés a hamarosan lejáró frakciókhoz
            for i, frakcio in enumerate(hamarosan_lejaro_frakciok):
                nev, kod, kezdet, lejarat, hozzaado = frakcio
                lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                
                # Csak a dátum részeket hasonlítjuk össze
                if lejarat_datum == ma:
                    allapot_szoveg = "🟡 MA lejár!"
                else:
                    hatralevo_napok = (lejarat_datum - ma).days
                    allapot_szoveg = f"🟡 Hamarosan lejár (még {hatralevo_napok} nap)"

                hamarosan_embed.add_field(
                    name=f"{nev} ({kod})",
                    value=f"Lejárat: {lejarat}\nÁllapot: {allapot_szoveg}",
                    inline=True
                )

                # Minden harmadik után üres mező a sorváltáshoz
                if (i + 1) % 3 == 0 and i < len(hamarosan_lejaro_frakciok) - 1:
                    hamarosan_embed.add_field(name="\u200b", value="\u200b", inline=False)

            await send_tracked_message(ctx, embed=hamarosan_embed)

        # Aktív frakciók embed
        if aktiv_frakciok:
            aktiv_embed = discord.Embed(title="Aktív Frakciók", color=discord.Color.green())

            # Három oszlopos elrendezés az aktív frakciókhoz
            for i, frakcio in enumerate(aktiv_frakciok):
                nev, kod, kezdet, lejarat, hozzaado = frakcio
                lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                hatralevo_napok = (lejarat_datum - ma).days

                aktiv_embed.add_field(
                    name=f"{nev} ({kod})",
                    value=f"Lejárat: {lejarat}\nÁllapot: 🟢 Aktív (még {hatralevo_napok} nap)",
                    inline=True
                )

                # Minden harmadik után üres mező a sorváltáshoz
                if (i + 1) % 3 == 0 and i < len(aktiv_frakciok) - 1:
                    aktiv_embed.add_field(name="\u200b", value="\u200b", inline=False)

            await send_tracked_message(ctx, embed=aktiv_embed)

        # Lejárt frakciók embed
        if lejart_frakciok:
            lejart_embed = discord.Embed(title="Lejárt Frakciók", color=discord.Color.red())

            # Három oszlopos elrendezés a lejárt frakciókhoz
            for i, frakcio in enumerate(lejart_frakciok):
                nev, kod, kezdet, lejarat, hozzaado = frakcio
                lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
                lejart_napok = (ma - lejarat_datum).days

                lejart_embed.add_field(
                    name=f"{nev} ({kod})",
                    value=f"Lejárat: {lejarat}\nÁllapot: 🔴 Lejárt ({lejart_napok} napja)",
                    inline=True
                )

                # Minden harmadik után üres mező a sorváltáshoz
                if (i + 1) % 3 == 0 and i < len(lejart_frakciok) - 1:
                    lejart_embed.add_field(name="\u200b", value="\u200b", inline=False)

            await send_tracked_message(ctx, embed=lejart_embed)

        # Ha nincs egy kategória sem
        if not hamarosan_lejaro_frakciok and not aktiv_frakciok and not lejart_frakciok:
            await send_tracked_message(ctx, content="Nincsenek frakciók az adatbázisban.")

    except Exception as e:
        logger.error(f"Hiba a frakciók listázásakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a frakciók listázásakor.\n{error_details}")

@bot.command(name="keres", aliases=PARANCSOK["KERES"])
@check_role()
async def keres(ctx, frakcio_nev: str):
    """Megkeres egy adott frakciót."""
    try:
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum, hozzaado_nev FROM frakciok WHERE nev = %s", (frakcio_nev,))
        frakcio = cursor.fetchone()

        if not frakcio:
            await send_tracked_message(ctx, content=f"A '{frakcio_nev}' nevű frakció nem található.")
            return

        nev, kod, kezdet, lejarat, hozzaado = frakcio

        # Lejárati dátum ellenőrzése
        lejarat_datum = datetime.strptime(lejarat, '%Y-%m-%d').date()
        ma = date.today()
        
        # Csak a dátum részeket hasonlítjuk össze
        if lejarat_datum < ma:
            status = "🔴 Lejárt"
        else:
            if lejarat_datum == ma:
                status = "🟡 MA lejár!"
            else:
                hatralevo_napok = (lejarat_datum - ma).days
                status = f"🟢 Aktív (még {hatralevo_napok} nap)"

        embed = discord.Embed(title=f"Frakció: {nev}", color=discord.Color.blue())
        embed.add_field(name="Kód", value=kod, inline=True)
        embed.add_field(name="Kezdet", value=kezdet, inline=True)
        embed.add_field(name="Lejárat", value=lejarat, inline=True)
        embed.add_field(name="Hozzáadta", value=hozzaado, inline=True)
        embed.add_field(name="Állapot", value=status, inline=True)

        await send_tracked_message(ctx, embed=embed)
    except Exception as e:
        logger.error(f"Hiba a frakció keresésekor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a frakció keresésekor.\n{error_details}")

@bot.command(name="hosszabbit", aliases=PARANCSOK["FRISSIT"])
@check_role()
async def hosszabbit(ctx, frakcio_nev: str, napok: int = 0, hetek: int = 0, konkret_datum: str = None):
    """Meghosszabbítja egy adott frakció szerződését."""
    try:
        # Ellenőrizzük, hogy legalább az egyik érték meg van adva
        if napok == 0 and hetek == 0 and konkret_datum is None:
            await send_tracked_message(ctx, content="Hiba: Legalább a napok, hetek számának, vagy a konkrét dátumnak meg kell lennie adva.")
            return

        # Ellenőrizzük, hogy csak az egyik érték van-e megadva
        megadott_ertekek = 0
        if napok > 0: megadott_ertekek += 1
        if hetek > 0: megadott_ertekek += 1
        if konkret_datum: megadott_ertekek += 1

        if megadott_ertekek > 1:
            await send_tracked_message(ctx, content="Hiba: Csak napokat VAGY heteket VAGY konkrét dátumot adhatsz meg, nem többet. Kérlek, csak az egyiket állítsd be.")
            return

        # Jelenlegi adatok lekérése
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT lejarat_datum FROM frakciok WHERE nev = %s", (frakcio_nev,))
        result = cursor.fetchone()

        if not result:
            await send_tracked_message(ctx, content=f"A '{frakcio_nev}' nevű frakció nem létezik.")
            return

        jelenlegi_lejarat = datetime.strptime(result[0], '%Y-%m-%d')
        ma = datetime.now()
        hozzaado_nev = ctx.author.name

        # Új lejárati dátum kiszámítása
        if konkret_datum:
            try:
                uj_lejarat = datetime.strptime(konkret_datum, '%Y-%m-%d')
                idotartam_szoveg = f"konkrét dátum: {konkret_datum}"
            except ValueError:
                await send_tracked_message(ctx, content="Hiba: A dátumnak ÉÉÉÉ-HH-NN formátumban kell lennie (pl. 2023-12-31).")
                return
        else:
            # Időtartam kiszámítása
            idotartam = timedelta(days=napok, weeks=hetek)

            if jelenlegi_lejarat > ma:
                # Ha még nem járt le, akkor a jelenlegi lejárati dátumhoz adjuk hozzá az új időtartamot
                uj_lejarat = jelenlegi_lejarat + idotartam
                kezdet_datum = jelenlegi_lejarat - timedelta(weeks=4)  # Becsült kezdet (nem változtatjuk az adatbázisban)
            else:
                # Ha már lejárt, akkor a mai dátumtól számítjuk
                kezdet_datum = ma
                uj_lejarat = ma + idotartam

            # Időtartam szöveg összeállítása
            if napok > 0:
                idotartam_szoveg = f"{napok} nap"
            else:
                idotartam_szoveg = f"{hetek} hét"

        # Megerősítés kérése
        embed = discord.Embed(
            title="Szerződés meghosszabbítása - Megerősítés",
            description=f"Biztosan meghosszabbítod ezt a szerződést?\n\n**Név:** {frakcio_nev}\n**Időtartam:** {idotartam_szoveg}\n**Jelenlegi lejárat:** {jelenlegi_lejarat.strftime('%Y-%m-%d')}\n**Új lejárat:** {uj_lejarat.strftime('%Y-%m-%d')}",  
            color=discord.Color.blue()
        )

        async def update_faction(ctx, frakcio_nev, uj_lejarat, hozzaado_nev):
            try:
                conn, cursor = ensure_connection()

                # Ha már lejárt és nem konkrét dátumot adtunk meg, frissítjük a kezdő dátumot is
                if jelenlegi_lejarat <= ma and konkret_datum is None:
                    execute_query(cursor,
                        "UPDATE frakciok SET kezdet_datum = %s, lejarat_datum = %s, hozzaado_nev = %s WHERE nev = %s",
                        (ma.strftime('%Y-%m-%d'), uj_lejarat.strftime('%Y-%m-%d'), hozzaado_nev, frakcio_nev)
                    )
                else:
                    # Ha még nem járt le, vagy konkrét dátumot adtunk meg, csak a lejárati dátumot frissítjük
                    execute_query(cursor,
                        "UPDATE frakciok SET lejarat_datum = %s, hozzaado_nev = %s WHERE nev = %s",
                        (uj_lejarat.strftime('%Y-%m-%d'), hozzaado_nev, frakcio_nev)
                    )

                conn.commit()

                embed = discord.Embed(
                    title="Szerződés meghosszabbítva",
                    description=f"A '{frakcio_nev}' frakció szerződése sikeresen meghosszabbítva: {uj_lejarat.strftime('%Y-%m-%d')}",
                    color=discord.Color.green()
                )
                await send_tracked_message(ctx, embed=embed)
            except Exception as e:
                logger.error(f"Hiba a szerződés meghosszabbításakor: {e}")
                error_details = format_error(e)
                await send_tracked_message(ctx, content=f"Hiba történt a szerződés meghosszabbításakor.\n{error_details}")

        view = ConfirmView(
            bot, 
            ctx, 
            "meghosszabbítás", 
            f"Frakció: {frakcio_nev}", 
            update_faction, 
            ctx, frakcio_nev, uj_lejarat, hozzaado_nev
        )
        await send_tracked_message(ctx, embed=embed, view=view)
    except Exception as e:
        logger.error(f"Hiba a szerződés meghosszabbításakor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a szerződés meghosszabbításakor.\n{error_details}")

@bot.command(name="torol", aliases=PARANCSOK["TOROL"])
@check_role()
async def torol(ctx, frakcio_nev: str):
    """Töröl egy adott frakciót."""
    try:
        conn, cursor = ensure_connection()
        execute_query(cursor, "SELECT nev, kod, kezdet_datum, lejarat_datum FROM frakciok WHERE nev = %s", (frakcio_nev,))
        frakcio = cursor.fetchone()

        if not frakcio:
            await send_tracked_message(ctx, content=f"A '{frakcio_nev}' nevű frakció nem található.")
            return

        nev, kod, kezdet, lejarat = frakcio

        embed = discord.Embed(
            title="Frakció törlése - Megerősítés",
            description=f"Biztosan törölni szeretnéd ezt a frakciót?\n\n**Név:** {nev}\n**Kód:** {kod}\n**Lejárat:** {lejarat}",
            color=discord.Color.red()
        )

        async def delete_faction(ctx, frakcio_nev):
            try:
                conn, cursor = ensure_connection()
                execute_query(cursor, "DELETE FROM frakciok WHERE nev = %s", (frakcio_nev,))
                conn.commit()

                embed = discord.Embed(
                    title="Frakció törölve",
                    description=f"A '{frakcio_nev}' nevű frakció sikeresen törölve.",
                    color=discord.Color.green()
                )
                await send_tracked_message(ctx, embed=embed)
            except Exception as e:
                logger.error(f"Hiba a frakció törlésekor: {e}")
                error_details = format_error(e)
                await send_tracked_message(ctx, content=f"Hiba történt a frakció törlésekor.\n{error_details}")

        view = ConfirmView(bot, ctx, "törlés", f"Frakció: {frakcio_nev}", delete_faction, ctx, frakcio_nev)
        await send_tracked_message(ctx, embed=embed, view=view)
    except Exception as e:
        logger.error(f"Hiba a törlés megerősítésekor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a törlés megerősítésekor.\n{error_details}")

@bot.command(name="szerkeszt", aliases=PARANCSOK["SZERKESZT"])
@check_role()
async def szerkeszt(ctx, frakcio_nev: str, uj_nev: str, uj_kod: str):
    """Szerkeszt egy adott frakciót."""
    try:
        conn, cursor = ensure_connection()
        execute_query(cursor,
            "UPDATE frakciok SET nev = %s, kod = %s WHERE nev = %s",
            (uj_nev, uj_kod, frakcio_nev)
        )
        conn.commit()

        embed = discord.Embed(
            title="Frakció szerkesztve",
            description=f"A frakció sikeresen módosítva.\n**Eredeti név:** {frakcio_nev}\n**Új név:** {uj_nev}",
            color=discord.Color.green()
        )
        await send_tracked_message(ctx, embed=embed)
    except Exception as e:
        error_details = format_error 
        logger.error(f"Hiba a frakció szerkesztésekor: {e}")
        error_details = format_error(e)
        await send_tracked_message(ctx, content=f"Hiba történt a frakció szerkesztésekor.\n{error_details}")

@bot.command(name="help", aliases=PARANCSOK["SEGITSEG"])
async def help(ctx):
    """Kiírja a parancsok részletes listáját."""
    embed = discord.Embed(
        title="Parancsok Részletes Leírása", 
        description="A bot által használható parancsok és funkcióik:", 
        color=discord.Color.blue()
    )
    
    for parancs, aliasok in PARANCSOK.items():
        # Parancs neve és aliasai
        parancs_nev = f"**{parancs}** ({', '.join(['$' + alias for alias in aliasok])})"
        
        # Parancs leírása
        leiras = PARANCS_LEIRASOK.get(parancs, "Nincs részletes leírás.")
        
        embed.add_field(name=parancs_nev, value=leiras, inline=False)
    
    await send_tracked_message(ctx, embed=embed)

# A fájl végén lévő bot.run() hívást módosítsuk, hogy használja a korábban elmentett BOT_TOKEN változót
# és adjon részletes hibaüzenetet, ha nincs beállítva

# Régi kód:
# token = os.getenv("BOT_TOKEN")
# if token is None:
#     logger.critical("BOT_TOKEN környezeti változó nincs beállítva! A bot nem tud elindulni.")
#     print("HIBA: A BOT_TOKEN környezeti változó nincs beállítva!")
#     print("Kérlek, állítsd be a BOT_TOKEN környezeti változót a Discord bot token értékével.")
#     print("Például: export BOT_TOKEN='a_te_token_értéked'")
# else:
#     bot.run(token)

# Új kód:
if BOT_TOKEN is None:
    logger.critical("BOT_TOKEN környezeti változó nincs beállítva! A bot nem tud elindulni.")
    print("HIBA: A BOT_TOKEN környezeti változó nem található!")
    print("Ellenőrizd, hogy a BOT_TOKEN környezeti változó be van-e állítva a Railway platformon.")
    print("Railway platformon: Project Settings -> Variables -> Add Variable")
    print("Név: BOT_TOKEN, Érték: a Discord bot tokenje")
    
else:
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.critical(f"Hiba a bot indításakor: {e}")
        print(f"HIBA a bot indításakor: {e}")
        print("Ellenőrizd, hogy a BOT_TOKEN értéke helyes-e.")
