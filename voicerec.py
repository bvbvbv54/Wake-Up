import speech_recognition as sr
import re
import time
from  datetime import datetime
from flask import Flask, jsonify, render_template_string, request, render_template, redirect, url_for, session
import sqlite3
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql.functions import current_user
from werkzeug.security import generate_password_hash, check_password_hash
import pygame
from threading import Lock
import  threading

# Initialize Flask app
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///C:/Users/moham/PycharmProjects/Wake-Up/test.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
app.secret_key = '98105116'
# Global variables
active_timers = {}
alarm_lock = threading.Lock()
active_users = {}
user_lock = Lock()
current_user_id = None
is_user_logged_in = False


# Function to recognize voice input
def get_voice_command():
    recognizer = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            print("Listening for an alarm command...")
            recognizer.adjust_for_ambient_noise(source, duration=1)
            recognizer.energy_threshold = 4000  # Adjust sensitivity
            recognizer.dynamic_energy_threshold = True
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)

        command = recognizer.recognize_google(audio).lower()
        print(f"Recognized: {command}")
        return command
    except sr.WaitTimeoutError:
        print("Timeout - No speech detected")
        return None
    except sr.UnknownValueError:
        print("Could not understand audio")
        return None
    except sr.RequestError as e:
        print(f"Speech service error: {e}")
        return None
    except Exception as e:
        print(f"Error in voice recognition: {e}")
        return None


# Extract time from voice command with improved pattern matching
def extract_time(command):
    # Pattern to match times like "2 pm", "2:30 pm", "14:30", etc.
    patterns = [
        r'(?:set|at)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)\b',  # 2:30 pm
        r'(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)\b',  # 2:30 pm without "set" or "at"
        r'(\d{1,2})(?::(\d{2}))?(?:\s*hours)?\b'  # 14:30 or 14 hours (24-hour format)
    ]

    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minutes = match.group(2) if match.group(2) else '00'

            # Handle 24-hour format
            if len(match.groups()) < 3 or match.group(3) is None:
                # Assume 24-hour format
                if 0 <= hour <= 23:
                    return f"{hour:02d}:{minutes}"
            else:
                # 12-hour format with AM/PM
                period = match.group(3).lower().replace('.', '')
                period = 'am' if 'a' in period else 'pm'

                # Convert to 12-hour format for display
                if period == 'pm' and hour < 12:
                    hour += 12
                elif period == 'am' and hour == 12:
                    hour = 0

                return f"{hour:02d}:{minutes}"

    return None


# Calculate seconds until alarm with improved handling
def get_seconds_until_alarm(alarm_time):
    now = datetime.now()

    # Handle both 24-hour and 12-hour formats
    try:
        if ' ' in alarm_time:  # 12-hour format with AM/PM
            alarm_dt = datetime.strptime(alarm_time, "%I:%M %p")
        else:  # 24-hour format
            alarm_dt = datetime.strptime(alarm_time, "%H:%M")
    except ValueError as e:
        print(f"Time format error: {e}")
        return None

    alarm_dt = alarm_dt.replace(year=now.year, month=now.month, day=now.day)

    # If alarm time is in the past, set it for tomorrow
    if alarm_dt < now:
        alarm_dt += datetime.timedelta(days=1)

    return (alarm_dt - now).total_seconds()


# Standardize alarm time format for storage
def standardize_time_format(time_str):
    # Convert to 12-hour format for consistency
    try:
        if ' ' in time_str:  # Already in 12-hour format
            dt = datetime.strptime(time_str, "%I:%M %p")
        else:  # 24-hour format
            dt = datetime.strptime(time_str, "%H:%M")

        return dt.strftime("%I:%M %p").lstrip('0')  # Remove leading zero for hour
    except ValueError:
        return time_str  # Return as is if format unknown


# Play alarm sound





# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    alarms = db.relationship('Alarm', backref='user', lazy=True)

# Alarm Model
class Alarm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    time = db.Column(db.String(5), nullable=False)  # Store time as "HH:MM"
    status = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

#Hom
@app.route('/')
def home():
    # Redirect to the login page
    return redirect(url_for('login'))

# Registration Route
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            return jsonify({"error": "Username and password are required"}), 400

        if User.query.filter_by(username=username).first():
            return jsonify({"error": "Username already exists"}), 400

        new_user = User(username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()

        return redirect(url_for('login'))

    return render_template('register.html')

#Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_id'] = user.id
            print(f"User {user.username} logged in")
            with user_lock:
                # Store user ID with thread identifier
                global current_user_id
                current_user_id = user.id

                # Redirect to the show_alarms page after successful login
            return redirect(url_for('dashboard', user_id=user.id))
        else:
            return jsonify({"error": "Invalid username or password"}), 401

    return render_template('login.html')
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    user = User.query.get_or_404(user_id)
    alarms = Alarm.query.filter_by(user_id=user_id).all()
    return render_template('dashboard.html', user_id=user_id,user=user, alarms=alarms)

@app.route('/logout', methods=['POST'])

def logout():
    global current_user_id
    with user_lock:
        if current_user_id:
            print(f"User {current_user_id} logged out")
            current_user_id = None  # Set user_id to None
            session.pop('user_id', None)  # Clear session data
            return jsonify({'status': 'success', 'message': 'Logged out successfully'})
        else:
            return jsonify({'status': 'error', 'message': 'No user is logged in'}), 400


# API to set the alarm
def create_alarm(user_id, alarm_time):
    try:
        formatted_time = standardize_time_format(alarm_time)
        if not formatted_time:
            return {"status": "error", "message": "Invalid time format"}

        # Check for existing alarms
        existing_alarm = Alarm.query.filter_by(
            user_id=user_id,
            time=formatted_time,
            status='active'
        ).first()

        if existing_alarm:
            return {
                "status": "error",
                "message": "Active alarm already exists",
                "alarm_id": existing_alarm.id
            }

        # Create new alarm
        new_alarm = Alarm(
            user_id=user_id,
            time=formatted_time,
            status='active'
        )
        db.session.add(new_alarm)
        db.session.commit()

        # Set timer (same code as before)
        # ... [timer logic from your original set_alarm] ...

        return {
            "status": "success",
            "message": "Alarm set",
            "alarm_id": new_alarm.id,
            "alarm_time": formatted_time
        }

    except Exception as e:
        db.session.rollback()
        return {"status": "error", "message": str(e)}


@app.route('/set_alarm', methods=['POST'])
def set_alarm_route():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or 'time' not in data:
        return jsonify({"error": "Missing time"}), 400

    result = create_alarm(session['user_id'], data['time'])
    status_code = 201 if result['status'] == 'success' else 400
    return jsonify(result), status_code

def play_alarm():
    try:
        print("⏰ Alarm is ringing! Wake up!")
        pygame.mixer.init()
        pygame.mixer.music.load("alarm.mp3")  # Replace with your sound file
        pygame.mixer.music.play()

        # Wait for the sound to finish playing
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
    except Exception as e:
        print(f"⚠️ Error playing alarm sound: {str(e)}")



# API to cancel an alarm
@app.route('/cancel_alarm/<int:alarm_id>', methods=['POST'])
def cancel_alarm_route(alarm_id):
    result = cancel_alarm(alarm_id)
    return jsonify(result)
def cancel_alarm(alarm_id):
    try:
        # Stop the timer if it exists
        with alarm_lock:
            if alarm_id in active_timers:
                timer = active_timers[alarm_id]
                timer.cancel()  # Stop the timer
                del active_timers[alarm_id]  # Remove the timer from the dictionary

        # Update the alarm status in the database using SQLAlchemy
        alarm = Alarm.query.get(alarm_id)  # Fetch the alarm by ID
        if alarm:
            alarm.status = 'cancelled'  # Update the status
            db.session.commit()  # Commit the changes to the database
            print(f"✅ Alarm {alarm_id} cancelled successfully!")
            return {"status": "success", "message": "Alarm cancelled", "alarm_id": alarm_id}
        else:
            print(f"⚠️ Alarm {alarm_id} not found in the database.")
            return {"status": "error", "message": "Alarm not found", "alarm_id": alarm_id}
    except Exception as e:
        print(f"⚠️ Error in cancel_alarm: {str(e)}")
        db.session.rollback()  # Rollback in case of an error
        return {"status": "error", "message": str(e), "alarm_id": alarm_id}


# Webpage displaying all alarms with improved UI
# @app.route('/alarms/<int:user_id>',methods=['GET'])
# def show_alarms(user_id):
#     user = User.query.get_or_404(user_id)
#     alarms = Alarm.query.filter_by(user_id=user_id).all()
#     print(alarms)
#     return render_template('dashboard.html', user_id=user_id,user=user, alarms=alarms)

@app.route('/alarms/<int:user_id>', methods=['GET'])
def show_alarms(user_id):
    user = User.query.get_or_404(user_id)
    alarms = Alarm.query.filter_by(
        user_id=user_id
    ).filter(
        Alarm.status != 'cancelled'  # Exclude canceled alarms
    ).order_by(
        Alarm.time.asc()  # Sort by time (earliest first)
    ).all()
    alarms_data = [{
        'id': alarm.id,
        'time': alarm.time,  # Replace with the actual field name
        'status': alarm.status  # Replace with the actual field name
    } for alarm in alarms]
    return jsonify({'alarms': alarms_data})

# Listen for voice commands
def listen_for_commands():
    print("Voice recognition system started")
    consecutive_errors = 0
    max_consecutive_errors = 5

    # Create application context for the thread
    with app.app_context():
        while True:
            try:
                with user_lock:
                    user_id = current_user_id  # Get current ID

                if user_id is None:
                    print("No active user - skipping voice command")
                    time.sleep(2)
                    continue

                voice_command = get_voice_command()

                if voice_command:
                    print(f"Voice command detected: {voice_command}")
                    consecutive_errors = 0  # Reset error counter

                    # Set alarm commands
                    if any(phrase in voice_command for phrase in ["set the alarm", "set an alarm", "set alarm"]):
                        alarm_time = extract_time(voice_command)
                        if alarm_time:
                            print(f"✅ Setting alarm for {alarm_time}")
                            result = create_alarm(user_id, alarm_time)
                            if 'error' in result.get('status', ''):
                                print(f"❌ Error: {result.get('message')}")
                            else:
                                print(f"✅ Alarm set successfully: {result}")
                        else:
                            print("❌ Could not extract time. Please include a specific time.")

                    # Cancel alarm commands
                    elif "cancel alarm" in voice_command or "delete alarm" in voice_command:
                        match = re.search(
                            r'(?:cancel|delete)\s+alarm\s+(?:number\s+|id\s+|#)?(\d+)',
                            voice_command,
                            re.IGNORECASE
                        )
                        if match:
                            alarm_id = int(match.group(1))
                            result = cancel_alarm(alarm_id)
                            if result.get('status') == 'success':
                                print(f"✅ Cancelled alarm {alarm_id}")
                            else:
                                print(f"❌ Error: {result.get('message')}")
                        else:
                            print("❌ Could not determine alarm ID. Please specify like 'Cancel alarm 3'")

                    # List alarms command
                    elif "list alarms" in voice_command or "show alarms" in voice_command:
                        alarms = Alarm.query.filter_by(user_id=user_id).order_by(Alarm.time).all()

                        if alarms:
                            print("\n📋 Current Alarms:")
                            for alarm in alarms:
                                print(f"ID: {alarm.id} | Time: {alarm.time} | Status: {alarm.status}")
                        else:
                            print("📋 No alarms currently set")

                    # Help command
                    elif "help" in voice_command:
                        print("\n🔧 Available Voice Commands:")
                        print("Set alarm [time] - Set a new alarm")
                        print("Cancel alarm [ID] - Cancel an existing alarm")
                        print("List alarms - Show all alarms")
                        print("Help - Show this help message")

                    else:
                        print("⚠️ Command not recognized. Say 'help' for available commands")

                else:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        print("⚠️ Too many consecutive errors. Resetting voice recognition...")
                        consecutive_errors = 0
                        time.sleep(2)  # Brief cooldown period

            except Exception as e:
                print(f"⚠️ Unexpected error: {str(e)}")
                consecutive_errors += 1
                time.sleep(1)

            time.sleep(0.5)  # Short pause between command checks
# Main function
def main():
    with app.app_context():
        db.create_all()
    voice_thread = threading.Thread(target=listen_for_commands, daemon=True)
    voice_thread.start()
    app.run(port=5000, debug=True, use_reloader=False)

if __name__ == "__main__":
    main()