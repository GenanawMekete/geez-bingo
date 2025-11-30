import os
import random
import logging
import json
import asyncio
import secrets
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://your-bingo-app.netlify.app')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
PORT = int(os.getenv('PORT', '8443'))

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
        self.game_stats = {
            'total_games': 0,
            'total_players': 0,
            'total_pot': 0
        }
        self.player_stats = {}
        self.available_cards = set(range(145, 545))  # 400 cards: 145-544
        self.card_cache = {}
        self.user_wallets = {}
        self.user_sessions = {}
        self.webapp_url = WEBAPP_URL
        self.auto_call_task = None
        
    def generate_card(self, card_number=None):
        """Generate a BINGO card with proper number ranges, seeded by card number"""
        if card_number is None:
            card_number = random.randint(145, 544)
            
        if card_number in self.card_cache:
            return self.card_cache[card_number].copy()
        
        # Use card number as seed for consistent generation
        random.seed(card_number)
        
        card = {}
        ranges = {'B': (1,15), 'I': (16,30), 'N': (31,45), 
                 'G': (46,60), 'O': (61,75)}
        
        for letter in 'BINGO':
            numbers = random.sample(range(ranges[letter][0], ranges[letter][1]+1), 5)
            card[letter] = numbers
        
        # Make center free
        card['N'][2] = "FREE"
        
        # Cache the card
        self.card_cache[card_number] = card.copy()
        
        # Reset random seed
        random.seed()
        
        return card
    
    def format_card_display(self, card, marked_numbers=None):
        """Format card for text display"""
        if marked_numbers is None:
            marked_numbers = set()
            
        lines = ["B   I   N   G   O", "--- --- --- --- ---"]
        
        for i in range(5):
            row = []
            for j, letter in enumerate('BINGO'):
                num = card[letter][i]
                if num == "FREE":
                    row.append(" * ")
                elif f"{letter}-{num}" in marked_numbers:
                    row.append(f"[{num:2}]")
                else:
                    row.append(f" {num:2} ")
            lines.append(" ".join(row))
        
        return "\n".join(lines)
    
    def format_card_for_webapp(self, card, card_number, marked_numbers=None):
        """Format card data for web app"""
        if marked_numbers is None:
            marked_numbers = set()
            
        card_data = {
            'card_number': card_number,
            'numbers': [],
            'marked': [],
            'free_position': 12  # Center position (0-indexed)
        }
        
        positions = []
        for i in range(5):
            for j, letter in enumerate('BINGO'):
                num = card[letter][i]
                if num == "FREE":
                    positions.append("FREE")
                else:
                    positions.append(num)
                    if f"{letter}-{num}" in marked_numbers:
                        card_data['marked'].append(len(positions) - 1)
        
        card_data['numbers'] = positions
        return card_data
    
    def get_user_wallet(self, user_id):
        """Get or initialize user wallet"""
        if user_id not in self.user_wallets:
            # Initialize with random balance between 150-200 like in the image
            self.user_wallets[user_id] = random.randint(150, 200)
        return self.user_wallets[user_id]
    
    def deduct_stake(self, user_id, amount):
        """Deduct stake from user wallet"""
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0
        
        if self.user_wallets[user_id] >= amount:
            self.user_wallets[user_id] -= amount
            return True
        return False
    
    def add_winnings(self, user_id, amount):
        """Add winnings to user wallet"""
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0
        self.user_wallets[user_id] += amount
    
    def generate_webapp_data(self, user_id):
        """Generate data to pre-populate the web app"""
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
        
        # Clean up old sessions (older than 1 hour)
        current_time = datetime.now()
        expired_sessions = []
        for sess_id, session_data in self.user_sessions.items():
            if (current_time - session_data['timestamp']).total_seconds() > 3600:
                expired_sessions.append(sess_id)
        
        for sess_id in expired_sessions:
            del self.user_sessions[sess_id]
        
        return {
            'session_id': session_id,
            'wallet': wallet,
            'stake': self.entry_fee,
            'available_cards': available_cards,
            'total_cards': len(available_cards),
            'game_active': self.game_active,
            'pot_amount': self.pot_amount,
            'win_pattern': self.win_pattern,
            'current_players': len(self.players)
        }
    
    def validate_session(self, session_id, user_id):
        """Validate web app session"""
        if session_id not in self.user_sessions:
            return False
        
        session = self.user_sessions[session_id]
        if session['user_id'] != user_id:
            return False
        
        if session['used']:
            return False
        
        # Check if session is expired (1 hour)
        if (datetime.now() - session['timestamp']).total_seconds() > 3600:
            del self.user_sessions[session_id]
            return False
        
        return True
    
    def check_win(self, player_data):
        """Check if player has BINGO based on current win pattern"""
        card = player_data['card']
        marked = player_data['marked']
        
        if self.win_pattern == "line":
            return self._check_line_win(card, marked)
        elif self.win_pattern == "full_house":
            return self._check_full_house(card, marked)
        elif self.win_pattern == "four_corners":
            return self._check_four_corners(card, marked)
        elif self.win_pattern == "X":
            return self._check_x_pattern(card, marked)
        elif self.win_pattern == "blackout":
            return self._check_blackout(card, marked)
        
        return False
    
    def _check_line_win(self, card, marked):
        """Check for any line (horizontal, vertical, diagonal)"""
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
    
    def _check_full_house(self, card, marked):
        """Check if all numbers are marked"""
        for letter in 'BINGO':
            for i in range(5):
                if not self._is_marked_or_free(card[letter][i], letter, marked):
                    return False
        return True
    
    def _check_four_corners(self, card, marked):
        """Check if four corners are marked"""
        corners = [
            ('B', 0), ('O', 0),  # Top corners
            ('B', 4), ('O', 4)   # Bottom corners
        ]
        return all(self._is_marked_or_free(card[letter][pos], letter, marked) for letter, pos in corners)
    
    def _check_x_pattern(self, card, marked):
        """Check X pattern (both diagonals)"""
        diag1 = all(self._is_marked_or_free(card[letter][i], letter, marked) for i, letter in enumerate('BINGO'))
        diag2 = all(self._is_marked_or_free(card[letter][4-i], letter, marked) for i, letter in enumerate('BINGO'))
        return diag1 and diag2
    
    def _check_blackout(self, card, marked):
        """Check if entire card is marked (same as full house)"""
        return self._check_full_house(card, marked)
    
    def _is_marked_or_free(self, num, letter, marked):
        """Check if number is marked or is free space"""
        return num == "FREE" or f"{letter}-{num}" in marked

    async def call_number_auto(self, context: CallbackContext):
        """Automatically call next number"""
        if not self.game_active or not self.auto_call_active:
            return
        
        called = await self.call_number_internal(context)
        if called in ["GAME_OVER", "WINNER"]:
            self.auto_call_active = False

    async def call_number_internal(self, context: CallbackContext):
        """Internal function to call a number"""
        # Generate available numbers
        available = set()
        ranges = {'B': (1,15), 'I': (16,30), 'N': (31,45), 
                 'G': (46,60), 'O': (61,75)}
        
        for letter, (start, end) in ranges.items():
            for num in range(start, end+1):
                call_str = f"{letter}-{num}"
                if call_str not in self.called_numbers:
                    available.add((letter, num))
        
        if not available:
            if context:
                await context.bot.send_message(
                    chat_id=self.admin_id,
                    text="🎉 All numbers called! Game over."
                )
            self.game_active = False
            return "GAME_OVER"
        
        # Call random number
        letter, number = random.choice(list(available))
        call_str = f"{letter}-{number}"
        self.called_numbers.add(call_str)
        
        # Update all players' marked numbers
        for player_data in self.players.values():
            if number in player_data['card'][letter]:
                player_data['marked'].add(call_str)
        
        # Send call announcement to all players
        header = f"""
# Geez Bingo

|    | Game {self.current_game_id} | Players {len(self.players)} | Bet {self.entry_fee} | Call {len(self.called_numbers)} |
|---|---|---|---|---|
| **B** | **I** | **N** | **G** | **O** |
---
## STARTED

### Current Call  
- {letter}-{number}
    """
        
        if context:
            for user_id in self.players:
                try:
                    await context.bot.send_message(chat_id=user_id, text=header)
                except Exception as e:
                    logger.error(f"Failed to send call to {user_id}: {e}")
        
        # Check for winners
        winners = []
        for user_id, player_data in self.players.items():
            if self.check_win(player_data):
                winners.append((user_id, player_data))
        
        if winners and context:
            self.auto_call_active = False
            for user_id, winner in winners:
                await self.declare_winner(context, user_id, winner)
            save_game_state()
            return "WINNER"
        
        return "CONTINUE"

    async def declare_winner(self, context: CallbackContext, user_id, winner):
        """Declare a winner and send notifications"""
        # Update stats
        self.game_stats['total_games'] += 1
        self.game_stats['total_pot'] += self.pot_amount
        
        if str(user_id) not in self.player_stats:
            self.player_stats[str(user_id)] = {'games_played': 0, 'games_won': 0, 'total_winnings': 0}
        
        self.player_stats[str(user_id)]['games_played'] += 1
        self.player_stats[str(user_id)]['games_won'] += 1
        self.player_stats[str(user_id)]['total_winnings'] += self.pot_amount
        
        # Add winnings to user's wallet
        self.add_winnings(user_id, self.pot_amount)
        
        # Send win message to all players
        card_display = self.format_card_display(winner['card'], winner['marked'])
        win_msg = f"""
## BINGO! 🎉

### 🏆 {winner['username']} has won the game!
💎 Pattern: {self.win_pattern}
💰 Pot: {self.pot_amount} coins
🎫 Card: #{winner['board_number']}

{card_display}

Numbers called: {len(self.called_numbers)}
    """
        
        for player_id in self.players:
            try:
                await context.bot.send_message(chat_id=player_id, text=win_msg)
            except Exception as e:
                logger.error(f"Failed to send win message to {player_id}: {e}")
        
        self.game_active = False
        # Reset pot for next game but keep players
        self.pot_amount = 0

# Initialize game
game = BingoGame()

def save_game_state():
    """Save game state to file"""
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
            'user_wallets': game.user_wallets,
            'user_sessions': game.user_sessions
        }
        with open('bingo_state.json', 'w') as f:
            json.dump(state, f, default=str)
        logger.info("Game state saved successfully")
    except Exception as e:
        logger.error(f"Error saving game state: {e}")

def load_game_state():
    """Load game state from file"""
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
        game.user_sessions = state.get('user_sessions', {})
        
        # Convert marked sets back from lists
        for player_id in game.players:
            if isinstance(game.players[player_id]['marked'], list):
                game.players[player_id]['marked'] = set(game.players[player_id]['marked'])
                
        logger.info("Game state loaded successfully")
    except FileNotFoundError:
        logger.info("No saved game state found")
    except Exception as e:
        logger.error(f"Error loading game state: {e}")

async def start(update: Update, context: CallbackContext):
    """Send welcome message when the command /start is issued."""
    user = update.effective_user
    
    # Set admin if not set
    if game.admin_id == 0:
        game.admin_id = user.id
        await update.message.reply_text("👑 You are now the game admin!")
    
    wallet = game.get_user_wallet(user.id)
    
    # Create Mini App button
    keyboard = [
        [InlineKeyboardButton(
            "🎮 Open Bingo Card Selector", 
            web_app=WebAppInfo(url=game.webapp_url)
        )],
        [InlineKeyboardButton("🎯 Quick Join", callback_data="quick_join")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
        [InlineKeyboardButton("💰 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📋 My Card", callback_data="my_card")],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = f"""
🎯 Welcome to Geez Bingo, {user.first_name}!

💰 Your Wallet: {wallet} coins
🎫 Available Cards: {len(game.available_cards)}
🏆 Current Pot: {game.pot_amount} coins
🎲 Win Pattern: {game.win_pattern}

Click "Open Bingo Card Selector" for the best experience!
Or use Quick Join for instant play.

Game Status: {'🟢 ACTIVE' if game.game_active else '🔴 INACTIVE'}
    """
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def webapp_start(update: Update, context: CallbackContext):
    """Start the web app for card selection"""
    user_id = update.effective_user.id
    username = update.effective_user.first_name
    
    if game.game_active:
        await update.message.reply_text("❌ Game is already in progress! Wait for the next one.")
        return
    
    if user_id in game.players:
        await update.message.reply_text("✅ You're already in the game!")
        return
    
    # Generate web app data
    webapp_data = game.generate_webapp_data(user_id)
    
    # Create web app button
    keyboard = [
        [InlineKeyboardButton(
            "🎮 Open Card Selector", 
            web_app=WebAppInfo(url=f"{game.webapp_url}?data={secrets.token_urlsafe(16)}")
        )]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🎯 {username}, click the button below to select your bingo card!\n\n"
        f"💰 Your wallet: {webapp_data['wallet']} coins\n"
        f"🎫 Available cards: {webapp_data['total_cards']}\n"
        f"🏆 Current pot: {game.pot_amount} coins\n"
        f"🎲 Win pattern: {game.win_pattern}",
        reply_markup=reply_markup
    )

async def handle_webapp_data(update: Update, context: CallbackContext):
    """Handle data sent from the web app"""
    try:
        if update.effective_message and update.effective_message.web_app_data:
            data = json.loads(update.effective_message.web_app_data.data)
            await process_webapp_selection(update, context, data)
    except Exception as e:
        logger.error(f"Error processing web app data: {e}")
        await update.effective_message.reply_text("❌ Error processing your selection. Please try again.")

async def process_webapp_selection(update: Update, context: CallbackContext, data: dict):
    """Process card selection from web app"""
    user_id = update.effective_user.id
    username = update.effective_user.first_name
    
    if data.get('action') == 'select_card':
        card_number = data['card_number']
        session_id = data.get('session_id')
        
        # Validate session if provided
        if session_id and not game.validate_session(session_id, user_id):
            await update.effective_message.reply_text("❌ Session expired or invalid. Please try again.")
            return
        
        # Mark session as used
        if session_id and session_id in game.user_sessions:
            game.user_sessions[session_id]['used'] = True
        
        # Process card selection
        await join_with_card(update, context, card_number, user_id, username)

async def join_with_card(update: Update, context: CallbackContext, card_number=None, user_id=None, username=None):
    """Join game with specific card"""
    if user_id is None:
        user_id = update.effective_user.id
    if username is None:
        username = update.effective_user.first_name
    
    if game.game_active:
        if context and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text("❌ Game is already in progress! Wait for the next one.")
        else:
            await context.bot.send_message(chat_id=user_id, text="❌ Game is already in progress! Wait for the next one.")
        return
    
    if user_id in game.players:
        if context and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text("✅ You're already in the game!")
        else:
            await context.bot.send_message(chat_id=user_id, text="✅ You're already in the game!")
        return
    
    # Check wallet
    wallet = game.get_user_wallet(user_id)
    if wallet < game.entry_fee:
        message = f"❌ Insufficient funds! You have {wallet} coins but need {game.entry_fee} coins."
        if context and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(message)
        else:
            await context.bot.send_message(chat_id=user_id, text=message)
        return
    
    # Handle card selection
    if card_number is None:
        if not game.available_cards:
            message = "❌ No cards available! Game is full."
            if context and hasattr(update, 'effective_message'):
                await update.effective_message.reply_text(message)
            else:
                await context.bot.send_message(chat_id=user_id, text=message)
            return
        card_number = random.choice(list(game.available_cards))
    
    # Validate card number
    if card_number not in game.available_cards:
        message = f"❌ Card #{card_number} is not available! Please select another card."
        if context and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(message)
        else:
            await context.bot.send_message(chat_id=user_id, text=message)
        return
    
    # Deduct stake
    if not game.deduct_stake(user_id, game.entry_fee):
        message = "❌ Transaction failed! Insufficient funds."
        if context and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(message)
        else:
            await context.bot.send_message(chat_id=user_id, text=message)
        return
    
    # Generate card and join game
    card = game.generate_card(card_number)
    game.players[user_id] = {
        'card': card,
        'marked': set(),
        'username': username,
        'board_number': card_number
    }
    
    game.available_cards.remove(card_number)
    game.pot_amount += game.entry_fee
    game.game_stats['total_players'] += 1
    
    # Send confirmation with formatted card display
    card_display = game.format_card_display(card)
    response = f"""
✅ {username} joined the game!

💰 Paid: {game.entry_fee} coins
🎫 Card: #{card_number}
🏆 Pot: {game.pot_amount} coins

{card_display}

Players: {len(game.players)}
Remaining Cards: {len(game.available_cards)}

Game will start when admin begins!
    """
    
    if context and hasattr(update, 'effective_message'):
        await update.effective_message.reply_text(response)
    else:
        await context.bot.send_message(chat_id=user_id, text=response)
    
    save_game_state()

async def show_my_card(update: Update, context: CallbackContext):
    """Show user's current card"""
    user_id = update.effective_user.id
    
    if user_id not in game.players:
        await update.message.reply_text("❌ You're not in the game! Use /join to play.")
        return
    
    player_data = game.players[user_id]
    card_display = game.format_card_display(player_data['card'], player_data['marked'])
    wallet = game.get_user_wallet(user_id)
    
    response = f"""
🎫 Your Bingo Card (#{player_data['board_number']})

{card_display}

💰 Wallet: {wallet} coins
✅ Marked: {len(player_data['marked'])} numbers
🎯 Total Called: {len(game.called_numbers)}
🎲 Game: #{game.current_game_id} ({'Active' if game.game_active else 'Inactive'})
    """
    
    # Add button to see marked numbers
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh Card", callback_data="my_card")],
        [InlineKeyboardButton("📊 Game Status", callback_data="status")],
        [InlineKeyboardButton("💰 My Wallet", callback_data="wallet")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(response, reply_markup=reply_markup)

async def wallet_info(update: Update, context: CallbackContext):
    """Show user wallet information"""
    user_id = update.effective_user.id
    wallet = game.get_user_wallet(user_id)
    username = update.effective_user.first_name
    
    # Get player stats if available
    stats_text = ""
    if str(user_id) in game.player_stats:
        stats = game.player_stats[str(user_id)]
        win_rate = (stats['games_won'] / stats['games_played'] * 100) if stats['games_played'] > 0 else 0
        stats_text = f"""
📈 Your Stats:
Games Played: {stats['games_played']}
Games Won: {stats['games_won']}
Win Rate: {win_rate:.1f}%
Total Winnings: {stats['total_winnings']} coins
        """
    
    response = f"""
💰 Wallet Information

👤 Player: {username}
💼 Balance: {wallet} coins
🎫 Stake: {game.entry_fee} coins
🏆 Current Pot: {game.pot_amount} coins
{stats_text}
{"✅ Sufficient funds to play!" if wallet >= game.entry_fee else "❌ Need more coins to play"}
    """
    
    keyboard = [
        [InlineKeyboardButton("🎯 Join Game", callback_data="webapp_start")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(response, reply_markup=reply_markup)

async def game_status(update: Update, context: CallbackContext):
    """Show current game status"""
    status_text = f"""
🎯 Game Status

Game: #{game.current_game_id}
Status: {'🟢 ACTIVE' if game.game_active else '🔴 INACTIVE'}
Players: {len(game.players)}
Pot: {game.pot_amount} coins
Numbers Called: {len(game.called_numbers)}
Win Pattern: {game.win_pattern}
Available Cards: {len(game.available_cards)}
Entry Fee: {game.entry_fee} coins
    """
    
    if game.game_active:
        status_text += f"\n⏰ Started: {game.game_start_time.strftime('%H:%M:%S')}"
        status_text += f"\n🔢 Auto-call: {'🟢 ON' if game.auto_call_active else '🔴 OFF'}"
    
    keyboard = []
    if update.effective_user.id == game.admin_id:
        if not game.game_active:
            keyboard.append([InlineKeyboardButton("🚀 Start Game", callback_data="admin_start")])
        else:
            keyboard.append([InlineKeyboardButton("🎲 Call Number", callback_data="admin_call")])
            keyboard.append([InlineKeyboardButton(f"⚡ Auto-call: {'ON' if game.auto_call_active else 'OFF'}", 
                                                callback_data="admin_autocall")])
            keyboard.append([InlineKeyboardButton("🛑 End Game", callback_data="admin_end")])
    
    keyboard.extend([
        [InlineKeyboardButton("🎯 My Card", callback_data="my_card")],
        [InlineKeyboardButton("💰 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(status_text, reply_markup=reply_markup)

async def statistics(update: Update, context: CallbackContext):
    """Show game statistics"""
    user_id = update.effective_user.id
    
    stats_text = f"""
📊 Geez Bingo Statistics

Total Games: {game.game_stats['total_games']}
Total Players: {game.game_stats['total_players']}
Total Pot Distributed: {game.game_stats['total_pot']} coins

Current Game:
Players: {len(game.players)}
Pot: {game.pot_amount} coins
Available Cards: {len(game.available_cards)}
    """
    
    # Add personal stats if available
    if str(user_id) in game.player_stats:
        personal = game.player_stats[str(user_id)]
        win_rate = (personal['games_won'] / personal['games_played'] * 100) if personal['games_played'] > 0 else 0
        stats_text += f"""
Your Stats:
Games Played: {personal['games_played']}
Games Won: {personal['games_won']}
Win Rate: {win_rate:.1f}%
Total Winnings: {personal['total_winnings']} coins
        """
    
    keyboard = [
        [InlineKeyboardButton("🎯 Join Game", callback_data="webapp_start")],
        [InlineKeyboardButton("📋 Game Status", callback_data="status")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(stats_text, reply_markup=reply_markup)

async def call_number(update: Update, context: CallbackContext):
    """Call next bingo number"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can call numbers!")
        return
    
    if not game.game_active:
        await update.message.reply_text("❌ No active game! Use /startgame first.")
        return
    
    await game.call_number_internal(context)

async def start_game(update: Update, context: CallbackContext):
    """Start the bingo game"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can start the game!")
        return
    
    if game.game_active:
        await update.message.reply_text("❌ Game is already running!")
        return
    
    if len(game.players) < 1:
        await update.message.reply_text("❌ Need at least 1 player to start!")
        return
    
    game.game_active = True
    game.called_numbers.clear()
    game.game_start_time = datetime.now()
    game.current_game_id += 1
    
    # Notify all players
    header = f"""
# Geez Bingo

| {game.entry_fee} | Wallet | Stake |
|---|---|---|
|    | {sum(game.user_wallets.values())} | {game.entry_fee} |

Game #{game.current_game_id} STARTED! 🚀
    """
    
    for user_id, player_data in game.players.items():
        try:
            await context.bot.send_message(chat_id=user_id, text=header)
            # Send their card
            card_display = game.format_card_display(player_data['card'])
            await context.bot.send_message(
                chat_id=user_id, 
                text=f"🎫 Your Card (#{player_data['board_number']}):\n{card_display}"
            )
        except Exception as e:
            logger.error(f"Failed to send start message to {user_id}: {e}")
    
    save_game_state()

async def end_game(update: Update, context: CallbackContext):
    """End current game"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can end the game!")
        return
    
    game.game_active = False
    game.auto_call_active = False
    game.called_numbers.clear()
    
    await update.message.reply_text("🛑 Game ended! Players and cards are kept for next game.")
    save_game_state()

async def reset_game(update: Update, context: CallbackContext):
    """Reset everything"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can reset the game!")
        return
    
    game.players.clear()
    game.called_numbers.clear()
    game.game_active = False
    game.auto_call_active = False
    game.available_cards = set(range(145, 545))
    game.pot_amount = 0
    # Keep user wallets and stats
    
    await update.message.reply_text("🔄 Game completely reset! All players cleared and cards available.")
    save_game_state()

async def admin_panel(update: Update, context: CallbackContext):
    """Admin control panel"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can access this!")
        return
    
    keyboard = [
        [InlineKeyboardButton("🚀 Start Game", callback_data="admin_start")],
        [InlineKeyboardButton("🎲 Call Number", callback_data="admin_call")],
        [InlineKeyboardButton(f"⚡ Auto-call: {'ON' if game.auto_call_active else 'OFF'}", 
                            callback_data="admin_autocall")],
        [InlineKeyboardButton("🎯 Change Pattern", callback_data="admin_pattern")],
        [InlineKeyboardButton("💰 Set Entry Fee", callback_data="admin_fee")],
        [InlineKeyboardButton("🛑 End Game", callback_data="admin_end")],
        [InlineKeyboardButton("🔄 Reset Game", callback_data="admin_reset")],
        [InlineKeyboardButton("📊 Game Stats", callback_data="stats")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_text = f"""
👑 Admin Panel

Game Status: {'ACTIVE 🟢' if game.game_active else 'INACTIVE 🔴'}
Players: {len(game.players)}
Pot: {game.pot_amount} coins
Pattern: {game.win_pattern}
Entry Fee: {game.entry_fee} coins
Auto-call: {'ON 🟢' if game.auto_call_active else 'OFF 🔴'}
Available Cards: {len(game.available_cards)}
    """
    await update.message.reply_text(status_text, reply_markup=reply_markup)

async def pattern_selection(update: Update, context: CallbackContext):
    """Select win pattern"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can change patterns!")
        return
    
    keyboard = [
        [InlineKeyboardButton("📏 Line", callback_data="pattern_line")],
        [InlineKeyboardButton("🏠 Full House", callback_data="pattern_full_house")],
        [InlineKeyboardButton("🔲 Four Corners", callback_data="pattern_four_corners")],
        [InlineKeyboardButton("❌ X Pattern", callback_data="pattern_x")],
        [InlineKeyboardButton("⚫ Blackout", callback_data="pattern_blackout")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎯 Select Win Pattern:\n\n"
        "📏 Line - Any line (row, column, diagonal)\n"
        "🏠 Full House - Entire card\n"
        "🔲 Four Corners - Four corners\n"
        "❌ X Pattern - Both diagonals\n"
        "⚫ Blackout - Entire card",
        reply_markup=reply_markup
    )

async def set_fee(update: Update, context: CallbackContext):
    """Set entry fee"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can set entry fee!")
        return
    
    if context.args and context.args[0].isdigit():
        game.entry_fee = int(context.args[0])
        await update.message.reply_text(f"💰 Entry fee set to {game.entry_fee} coins")
        save_game_state()
    else:
        await update.message.reply_text("Usage: /setfee <amount>")

async def broadcast(update: Update, context: CallbackContext):
    """Broadcast message to all players"""
    if update.effective_user.id != game.admin_id:
        await update.message.reply_text("❌ Only the admin can broadcast!")
        return
    
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    for user_id in game.players:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📢 Admin: {message}")
        except Exception as e:
            logger.error(f"Failed to broadcast to {user_id}: {e}")
    
    await update.message.reply_text("✅ Message broadcasted to all players!")

async def button_handler(update: Update, context: CallbackContext):
    """Handle inline button presses"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.first_name
    
    if data == "main_menu":
        await start(update, context)
    elif data == "webapp_start":
        await webapp_start(update, context)
    elif data == "quick_join":
        await join_with_card(update, context, user_id=user_id, username=username)
    elif data == "my_card":
        await show_my_card(update, context)
    elif data == "wallet":
        await wallet_info(update, context)
    elif data == "status":
        await game_status(update, context)
    elif data == "stats":
        await statistics(update, context)
    elif data.startswith("admin_"):
        if user_id != game.admin_id:
            await query.edit_message_text("❌ Only the admin can use these buttons!")
            return
        
        if data == "admin_start":
            await start_game(update, context)
            await admin_panel(update, context)
        elif data == "admin_call":
            await call_number(update, context)
            await admin_panel(update, context)
        elif data == "admin_autocall":
            game.auto_call_active = not game.auto_call_active
            status = "ON 🟢" if game.auto_call_active else "OFF 🔴"
            if game.auto_call_active and game.game_active:
                context.job_queue.run_repeating(
                    game.call_number_auto, 
                    interval=10,
                    first=5
                )
            await query.edit_message_text(f"⚡ Auto-call is now {status}")
            await admin_panel(update, context)
        elif data == "admin_pattern":
            await pattern_selection(update, context)
        elif data == "admin_end":
            await end_game(update, context)
            await admin_panel(update, context)
        elif data == "admin_reset":
            await reset_game(update, context)
            await admin_panel(update, context)
        elif data == "admin_panel":
            await admin_panel(update, context)
        elif data == "admin_fee":
            await query.edit_message_text("Use /setfee <amount> to change entry fee")
    elif data.startswith("pattern_"):
        pattern = data.replace("pattern_", "")
        pattern_names = {
            "line": "Line",
            "full_house": "Full House", 
            "four_corners": "Four Corners",
            "x": "X Pattern",
            "blackout": "Blackout"
        }
        game.win_pattern = pattern
        await query.edit_message_text(f"🎯 Win pattern set to: {pattern_names[pattern]}")
        save_game_state()

# Command handlers
async def join_command(update: Update, context: CallbackContext):
    """Handle /join command"""
    await webapp_start(update, context)

async def card_command(update: Update, context: CallbackContext):
    """Handle /card command"""
    await show_my_card(update, context)

async def wallet_command(update: Update, context: CallbackContext):
    """Handle /wallet command"""
    await wallet_info(update, context)

async def status_command(update: Update, context: CallbackContext):
    """Handle /status command"""
    await game_status(update, context)

async def stats_command(update: Update, context: CallbackContext):
    """Handle /stats command"""
    await statistics(update, context)

async def admin_command(update: Update, context: CallbackContext):
    """Handle /admin command"""
    await admin_panel(update, context)

def main():
    """Start the bot."""
    # Load saved game state
    load_game_state()
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is required!")
        return
    
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("webapp", webapp_start))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("card", card_command))
    application.add_handler(CommandHandler("wallet", wallet_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin", admin_command))
    
    # Web app data handler
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    
    # Admin commands
    application.add_handler(CommandHandler("startgame", start_game))
    application.add_handler(CommandHandler("call", call_number))
    application.add_handler(CommandHandler("endgame", end_game))
    application.add_handler(CommandHandler("reset", reset_game))
    application.add_handler(CommandHandler("setfee", set_fee))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("pattern", pattern_selection))

    # Button handler
    application.add_handler(CallbackQueryHandler(button_handler))

    # Start the Bot
    logger.info("🎯 Geez Bingo Bot with Web App is running...")
    logger.info(f"🃏 Available cards: {len(game.available_cards)}")
    logger.info(f"🌐 Web app URL: {game.webapp_url}")
    
    # For production with webhooks
    if os.getenv('RAILWAY_STATIC_URL') or os.getenv('WEBHOOK_URL'):
        webhook_url = os.getenv('RAILWAY_STATIC_URL') or os.getenv('WEBHOOK_URL')
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}"
        )
    else:
        # For development
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()