import os
import random
import logging
import json
import asyncio
import secrets
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://abush-bingo-bot-webapp.netlify.app')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
PORT = int(os.getenv('PORT', '8000'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class BingoGame:
    def __init__(self):
        self.players = {}
        self.called_numbers = set()
        self.game_active = False
        self.auto_call_active = False
        self.current_game_id = 1
        self.game_start_time = None
        self.admin_id = ADMIN_ID
        self.pot_amount = 0
        self.entry_fee = 10
        self.win_pattern = "line"
        self.game_stats = {'total_games': 0, 'total_players': 0, 'total_pot': 0}
        self.player_stats = {}
        self.available_cards = set(range(145, 545))
        self.card_cache = {}
        self.user_wallets = {}
        self.user_sessions = {}
        self.webapp_url = WEBAPP_URL
        
    def generate_card(self, card_number=None):
        if card_number is None:
            card_number = random.randint(145, 544)
            
        if card_number in self.card_cache:
            return self.card_cache[card_number].copy()
        
        random.seed(card_number)
        card = {}
        ranges = {'B': (1,15), 'I': (16,30), 'N': (31,45), 'G': (46,60), 'O': (61,75)}
        
        for letter in 'BINGO':
            numbers = random.sample(range(ranges[letter][0], ranges[letter][1]+1), 5)
            card[letter] = numbers
        
        card['N'][2] = "FREE"
        self.card_cache[card_number] = card.copy()
        random.seed()
        return card
    
    def format_card_display(self, card, marked_numbers=None):
        if marked_numbers is None:
            marked_numbers = set()
            
        lines = ["B   I   N   G   O", "--- --- --- --- ---"]
        
        for i in range(5):
            row = []
            for letter in 'BINGO':
                num = card[letter][i]
                if num == "FREE":
                    row.append(" * ")
                elif f"{letter}-{num}" in marked_numbers:
                    row.append(f"[{num:2}]")
                else:
                    row.append(f" {num:2} ")
            lines.append(" ".join(row))
        
        return "\n".join(lines)
    
    def get_user_wallet(self, user_id):
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = random.randint(150, 200)
        return self.user_wallets[user_id]
    
    def deduct_stake(self, user_id, amount):
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0
        if self.user_wallets[user_id] >= amount:
            self.user_wallets[user_id] -= amount
            return True
        return False
    
    def add_winnings(self, user_id, amount):
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0
        self.user_wallets[user_id] += amount
    
    def generate_webapp_data(self, user_id):
        wallet = self.get_user_wallet(user_id)
        available_cards = sorted(list(self.available_cards))
        
        session_id = secrets.token_hex(16)
        self.user_sessions[session_id] = {
            'user_id': user_id,
            'wallet': wallet,
            'available_cards': available_cards,
            'timestamp': datetime.now(),
            'used': False
        }
        
        return {
            'session_id': session_id,
            'wallet': wallet,
            'stake': self.entry_fee,
            'available_cards': available_cards,
            'total_cards': len(available_cards),
            'game_active': self.game_active,
            'pot_amount': self.pot_amount,
            'win_pattern': self.win_pattern
        }
    
    def check_win(self, player_data):
        card = player_data['card']
        marked = player_data['marked']
        
        # Check rows
        for i in range(5):
            if all(self._is_marked_or_free(card[letter][i], letter, marked) for letter in 'BINGO'):
                return True
        
        # Check columns
        for letter in 'BINGO':
            if all(self._is_marked_or_free(card[letter][i], letter, marked) for i in range(5)):
                return True
        
        # Check diagonals
        if all(self._is_marked_or_free(card[letter][i], letter, marked) for i, letter in enumerate('BINGO')):
            return True
        if all(self._is_marked_or_free(card[letter][4-i], letter, marked) for i, letter in enumerate('BINGO')):
            return True
        
        return False
    
    def _is_marked_or_free(self, num, letter, marked):
        return num == "FREE" or f"{letter}-{num}" in marked

# Initialize game
game = BingoGame()

def save_game_state():
    try:
        state = {
            'players': game.players,
            'called_numbers': list(game.called_numbers),
            'game_active': game.game_active,
            'current_game_id': game.current_game_id,
            'pot_amount': game.pot_amount,
            'game_stats': game.game_stats,
            'player_stats': game.player_stats,
            'available_cards': list(game.available_cards),
            'user_wallets': game.user_wallets
        }
        with open('bingo_state.json', 'w') as f:
            json.dump(state, f, default=str)
    except Exception as e:
        logger.error(f"Error saving game state: {e}")

def load_game_state():
    try:
        with open('bingo_state.json', 'r') as f:
            state = json.load(f)
        
        game.players = state['players']
        game.called_numbers = set(state['called_numbers'])
        game.game_active = state['game_active']
        game.current_game_id = state['current_game_id']
        game.pot_amount = state.get('pot_amount', 0)
        game.game_stats = state.get('game_stats', game.game_stats)
        game.player_stats = state.get('player_stats', {})
        game.available_cards = set(state.get('available_cards', range(145, 545)))
        game.user_wallets = state.get('user_wallets', {})
        
        for player_id in game.players:
            if isinstance(game.players[player_id]['marked'], list):
                game.players[player_id]['marked'] = set(game.players[player_id]['marked'])
    except FileNotFoundError:
        logger.info("No saved game state found")

async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    
    if game.admin_id == 0:
        game.admin_id = user.id
    
    wallet = game.get_user_wallet(user.id)
    
    keyboard = [
        [InlineKeyboardButton("🎮 Open Card Selector", web_app=WebAppInfo(url=game.webapp_url))],
        [InlineKeyboardButton("🎯 Quick Join", callback_data="quick_join")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
        [InlineKeyboardButton("💰 My Wallet", callback_data="wallet")],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = f"""
🎯 Welcome to Geez Bingo, {user.first_name}!

💰 Wallet: {wallet} coins
🎫 Available Cards: {len(game.available_cards)}
🏆 Current Pot: {game.pot_amount} coins

Game Status: {'🟢 ACTIVE' if game.game_active else '🔴 INACTIVE'}
    """
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def handle_webapp_data(update: Update, context: CallbackContext):
    try:
        if update.effective_message and update.effective_message.web_app_data:
            data = json.loads(update.effective_message.web_app_data.data)
            await process_webapp_selection(update, context, data)
    except Exception as e:
        logger.error(f"Error processing web app data: {e}")

async def process_webapp_selection(update: Update, context: CallbackContext, data: dict):
    user_id = update.effective_user.id
    username = update.effective_user.first_name
    
    if data.get('action') == 'select_card':
        card_number = data['card_number']
        await join_with_card(update, context, card_number, user_id, username)

async def join_with_card(update: Update, context: CallbackContext, card_number=None, user_id=None, username=None):
    if user_id is None:
        user_id = update.effective_user.id
    if username is None:
        username = update.effective_user.first_name
    
    if game.game_active:
        await update.effective_message.reply_text("❌ Game is already in progress!")
        return
    
    if user_id in game.players:
        await update.effective_message.reply_text("✅ You're already in the game!")
        return
    
    wallet = game.get_user_wallet(user_id)
    if wallet < game.entry_fee:
        await update.effective_message.reply_text(f"❌ Need {game.entry_fee} coins! You have {wallet}.")
        return
    
    if card_number is None:
        if not game.available_cards:
            await update.effective_message.reply_text("❌ No cards available!")
            return
        card_number = random.choice(list(game.available_cards))
    
    if card_number not in game.available_cards:
        await update.effective_message.reply_text(f"❌ Card #{card_number} not available!")
        return
    
    if not game.deduct_stake(user_id, game.entry_fee):
        await update.effective_message.reply_text("❌ Transaction failed!")
        return
    
    card = game.generate_card(card_number)
    game.players[user_id] = {
        'card': card,
        'marked': set(),
        'username': username,
        'board_number': card_number
    }
    
    game.available_cards.remove(card_number)
    game.pot_amount += game.entry_fee
    
    card_display = game.format_card_display(card)
    response = f"""
✅ {username} joined!

💰 Paid: {game.entry_fee} coins
🎫 Card: #{card_number}
🏆 Pot: {game.pot_amount} coins

{card_display}

Players: {len(game.players)}
    """
    await update.effective_message.reply_text(response)
    save_game_state()

async def call_number(update: Update, context: CallbackContext):
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Admin only!")
        return
    
    if not game.game_active:
        await update.message.reply_text("❌ No active game!")
        return
    
    available = set()
    ranges = {'B': (1,15), 'I': (16,30), 'N': (31,45), 'G': (46,60), 'O': (61,75)}
    
    for letter, (start, end) in ranges.items():
        for num in range(start, end+1):
            call_str = f"{letter}-{num}"
            if call_str not in game.called_numbers:
                available.add((letter, num))
    
    if not available:
        await update.message.reply_text("🎉 All numbers called!")
        game.game_active = False
        return
    
    letter, number = random.choice(list(available))
    call_str = f"{letter}-{number}"
    game.called_numbers.add(call_str)
    
    for player_data in game.players.values():
        if number in player_data['card'][letter]:
            player_data['marked'].add(call_str)
    
    header = f"""
# Geez Bingo

Game {game.current_game_id} | Players {len(game.players)} | Call {len(game.called_numbers)}

### Current Call: {letter}-{number}
    """
    
    for user_id in game.players:
        try:
            await context.bot.send_message(chat_id=user_id, text=header)
        except Exception as e:
            logger.error(f"Failed to send to {user_id}: {e}")
    
    winners = []
    for user_id, player_data in game.players.items():
        if game.check_win(player_data):
            winners.append((user_id, player_data))
    
    if winners:
        for user_id, winner in winners:
            await declare_winner(context, user_id, winner)
        save_game_state()

async def declare_winner(context: CallbackContext, user_id, winner):
    game.game_stats['total_games'] += 1
    game.game_stats['total_pot'] += game.pot_amount
    
    if str(user_id) not in game.player_stats:
        game.player_stats[str(user_id)] = {'games_played': 0, 'games_won': 0, 'total_winnings': 0}
    
    game.player_stats[str(user_id)]['games_played'] += 1
    game.player_stats[str(user_id)]['games_won'] += 1
    game.player_stats[str(user_id)]['total_winnings'] += game.pot_amount
    
    game.add_winnings(user_id, game.pot_amount)
    
    card_display = game.format_card_display(winner['card'], winner['marked'])
    win_msg = f"""
## BINGO! 🎉

🏆 {winner['username']} won!
💰 Pot: {game.pot_amount} coins
🎫 Card: #{winner['board_number']}

{card_display}
    """
    
    for player_id in game.players:
        try:
            await context.bot.send_message(chat_id=player_id, text=win_msg)
        except Exception as e:
            logger.error(f"Failed to send win to {player_id}: {e}")
    
    game.game_active = False
    game.pot_amount = 0

async def start_game(update: Update, context: CallbackContext):
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Admin only!")
        return
    
    if game.game_active:
        await update.message.reply_text("❌ Game already running!")
        return
    
    if len(game.players) < 1:
        await update.message.reply_text("❌ Need players!")
        return
    
    game.game_active = True
    game.called_numbers.clear()
    game.current_game_id += 1
    
    header = f"""
# Geez Bingo

Game #{game.current_game_id} STARTED! 🚀
Players: {len(game.players)}
Pot: {game.pot_amount} coins
    """
    
    for user_id, player_data in game.players.items():
        try:
            await context.bot.send_message(chat_id=user_id, text=header)
            card_display = game.format_card_display(player_data['card'])
            await context.bot.send_message(chat_id=user_id, text=f"🎫 Your Card (#{player_data['board_number']}):\n{card_display}")
        except Exception as e:
            logger.error(f"Failed to send to {user_id}: {e}")
    
    save_game_state()

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "quick_join":
        user_id = query.from_user.id
        username = query.from_user.first_name
        await join_with_card(update, context, user_id=user_id, username=username)
    elif data == "stats":
        stats_text = f"""
📊 Statistics

Games: {game.game_stats['total_games']}
Players: {game.game_stats['total_players']}
Total Pot: {game.game_stats['total_pot']} coins
        """
        await query.edit_message_text(stats_text)
    elif data == "wallet":
        user_id = query.from_user.id
        wallet = game.get_user_wallet(user_id)
        await query.edit_message_text(f"💰 Wallet: {wallet} coins")

def main():
    load_game_state()
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN required!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("join", start))
    application.add_handler(CommandHandler("startgame", start_game))
    application.add_handler(CommandHandler("call", call_number))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Webhook for production
    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        # Polling for development
        application.run_polling()

if __name__ == '__main__':
    main()
