import speech_recognition as sr
import re
import time
import uuid
from datetime import datetime, timedelta
from flask import Flask, jsonify,  request, render_template, redirect, url_for, session
import firebase_admin
from firebase_admin import db, auth
from threading import Lock
import threading
import traceback
from flask_cors import CORS
import os
import requests
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
CORS(app)

# Initialize Flask app
app = Flask(__name__)
if os.environ.get("FLASK_ENV") == "production":
    app.config['SESSION_COOKIE_SECURE'] = True
else:
    app.config['SESSION_COOKIE_SECURE'] = False
# Initialize Firebase
cred = firebase_admin.credentials.Certificate('credentials.json')
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://wake-up-44e5a-default-rtdb.europe-west1.firebasedatabase.app/',
})
app.secret_key = '98105116'
# Global variables
active_timers = {}
alarm_lock = threading.Lock()
active_users = {}
user_lock = Lock()
current_user_id = None
is_user_logged_in = False
last_command = ""
wake_word_detected=False
sleep_start = None
sleep_start_times = {}

# Firebase database reference
root_ref = db.reference()


def create_user(email, password):
    try:
        print("\n[create_user] 1. Starting user creation...")

        # Create Firebase Auth user
        user = auth.create_user(
            email=email,
            password=password,
            email_verified=False,
            disabled=False
        )
        print(f"[create_user] 2. Auth user created - UID: {user.uid}")

        # Create initial user data in Realtime DB
        user_ref = db.reference(f'users/{user.uid}')
        user_data = {
            'email': email,
            'created_at': datetime.utcnow().isoformat(),
            'alarms': {},
            'hardware': {'pressure': 0, 'motor': 0},
            'sessions': {},
        }
        user_ref.set(user_data)
        print("[create_user] 3. Database record created")

        # ✅ Generate fake sleep sessions
        # generate_random_sessions(user.uid, num_sessions=5)

        return user

    except auth.EmailAlreadyExistsError:
        print("Error: Email already exists")
        return None
    except Exception as e:
        print(f"Error creating user: {str(e)}")
        return None


def get_user(user_id):
    try:
        return auth.get_user(user_id)
    except:
        return None


# getting the alarms from the firebase
def get_all_alarms(user_id):
    try:
        # Reference to the user's alarms in Firebase
        alarms_ref = db.reference(f'users/{user_id}/alarms')

        # Retrieve all alarms
        raw_alarms = alarms_ref.get()

        # Filter and sort alarms
        active_alarms = {
            aid: alarm for aid, alarm in raw_alarms.items()
            if alarm.get('status') != 'cancelled'
        }

        return active_alarms

    except Exception as e:
        print(f"Error retrieving alarms: {str(e)}")
        return {}


@app.route("/get_alarms", methods=["GET"])
def get_alarms():
    try:
        user_id = session.get('user_id')
        alarms_data = get_all_alarms(user_id)

        if not alarms_data:
            return jsonify({'status': 'error', 'message': 'No alarms found'}), 404

        # Convert dictionary to list of alarms
        alarms_list = []
        for alarm_id, alarm_info in alarms_data.items():
            alarms_list.append({
                'id': alarm_id,
                'time': alarm_info.get('time'),
                'status': alarm_info.get('status', 'active')
            })

        return jsonify({'status': 'success', 'alarms': alarms_list}), 200

    except Exception as e:
        print(f"Error fetching alarms: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Get current time in Tunisia (for comparison)
def get_tunisia_time():
    tunisia = pytz.timezone("Africa/Cairo")
    utc_now = datetime.utcnow()
    return (datetime.utcnow() + timedelta(hours=1)).strftime("%H:%M:%S")  # Matches DB format: "18:36:50"

# Extract time from user command
def extract_time(command):
    # Match "2:30 pm", "14:30", etc.
    patterns = [
        r'(?:set|at)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)\b',     # 2:30 pm
        r'(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)\b',                   # 2:30 pm no "set"
        r'(\d{1,2})(?::(\d{2}))?(?:\s*hours)?\b'                          # 14:30 or 14 hours
    ]

    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minutes = match.group(2) if match.group(2) else '00'

            if len(match.groups()) < 3 or match.group(3) is None:
                # 24-hour format
                if 0 <= hour <= 23:
                    return f"{hour:02d}:{minutes}:00"  # Add seconds
            else:
                # 12-hour format
                period = match.group(3).lower().replace('.', '')
                if 'p' in period and hour < 12:
                    hour += 12
                elif 'a' in period and hour == 12:
                    hour = 0
                return f"{hour:02d}:{minutes}:00"  # Add seconds

    return None

# Standardize any time input to "HH:MM:SS"
def standardize_time_format(time_str):
    try:
        if 'am' in time_str.lower() or 'pm' in time_str.lower():
            dt = datetime.strptime(time_str, "%I:%M %p")
        else:
            dt = datetime.strptime(time_str, "%H:%M")

        return dt.strftime("%H:%M:%S")
    except ValueError:
        return time_str


def check_and_activate_motor(user_uid):
    try:
        # Reference to user data
        user_ref = db.reference(f'users/{user_uid}')
        user_data = user_ref.get()

        if not user_data:
            print(f"User {user_uid} not found.")
            return

        hardware = user_data.get('hardware', {})
        pressure = hardware.get('pressure', 0)

        alarms = user_data.get('alarms', {})

        # Get current time in HH:MM:SS format (match your alarm time format)
        now =get_tunisia_time()
        print(now)
        for alarm_id, alarm in alarms.items():
            status = alarm.get('status')
            alarm_time = alarm.get('time')

            # Conditions to check
            if status == 'active' and pressure == 1 and alarm_time == now:
                print(f"⏰ Alarm triggered at {now} for user {user_uid}")

                # Update motor to 1
                user_ref.child('hardware/motor').set(1)
                print("✅ Motor updated to 1.")
                # Schedule motor to turn off after 10 seconds
                scheduler.add_job(lambda: user_ref.child('hardware/motor').set(0),
                                  'date', run_date=datetime.now() + timedelta(seconds=10))

                print("🕓 Motor will turn OFF in 10 seconds.")
                break  # Optional: stop after first match

    except Exception as e:
        print(f"⚠️ Error during alarm check: {str(e)}")

def start_scheduler():
        global scheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(lambda: check_and_activate_motor(current_user_id), 'interval', seconds=1)
        scheduler.start()
# Home
@app.route('/')
def index():
    return render_template('index.html', time=datetime.now().timestamp())


# Registration Route - modified to verify the user data in the database
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        print("\n=== DEBUG START ===")
        print(f"1. Form Data Received - Email: {email}, Password: {password}")

        if not email or not password:
            print("2. Validation Failed - Empty email or password")
            return render_template('register.html', error="Email and password are required")

        try:
            print("3. Attempting to create Firebase user...")
            user = create_user(email, password)

            if user:
                print(f"4. User Created Successfully - UID: {user.uid}")
                print("5. Verifying database write...")
                user_data = db.reference(f'users/{user.uid}').get()
                print('luser data: ', user_data)
                if user_data:
                    print(f"6. Database Verification SUCCESS - Data: {user_data}")
                    return redirect(url_for('login'))
                else:
                    print("6. Database Verification FAILED - No data found")
                    return render_template('register.html',
                                           error="Registration incomplete - please try again")

            print("4. User Creation Returned None")
            return render_template('register.html', error="Registration failed")

        # except auth.EmailAlreadyExistsError:
        #     print("3. ERROR - Email already exists")
        #     return render_template('register.html', error="Email already registered")
        # except auth.WeakPasswordError:
        #     print("3. ERROR - Password too weak")
        #     return render_template('register.html', error="Password must be 6+ characters")
        except Exception as e:
            print(f"3. EXCEPTION OCCURRED: {str(e)}")
            return render_template('register.html', error=str(e))

    print("=== DEBUG - GET Request ===")
    return render_template('register.html')


# Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Get form data
        print("\n=== LOGIN DEBUG START ===")
        email = request.form.get('email')
        password = request.form.get('password')
        print(f"1. Login attempt - Email: {email}")

        # Check if fields are empty
        if not email or not password:
            print("ERROR: Missing email or password")
            return render_template('login.html', error="Both fields are required.")

        try:
            # Attempt to get user from Firebase
            print("2. Verifying user with Firebase...")
            user = auth.get_user_by_email(email)
            print(f"3. User found - UID: {user.uid}")

            # Set session data securely
            print("4. Setting session data...")
            session['user_id'] = user.uid
            session.permanent = True  # Enable session timeout

            # Update global user tracking
            print("5. Updating global user state...")
            with user_lock:
                global current_user_id
                current_user_id = user.uid
            try:
                print("6a. Updating Firebase 'current-user' node...")
                db.reference('current-user').set(user.uid)
                print("6b. Firebase 'current-user' updated successfully.")
            except Exception as e:
                print(f"ERROR: Failed to update 'current-user' in Firebase - {str(e)}")

            print("6. Login successful - Redirecting to dashboard")
            return redirect(url_for('dash'))

        except auth.UserNotFoundError:
            print("3. ERROR: User not found in Firebase")
            return render_template('login.html', error="User not found. Please check your credentials.")
        except Exception as e:
            print(f"3. ERROR: {str(e)}")
            traceback.print_exc()  # Print full error trace
            return render_template('login.html', error="An error occurred. Please try again later.")

    print("=== DEBUG - GET Request ===")
    return render_template('login.html')


@app.route('/dash')
def dash():
    # Ensure the user is logged in, otherwise redirect to login
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']  # ✅ Now it's safe to access

    alarms = get_all_alarms(user_id)
    if alarms is None:
        alarms = {}

    user_ref = db.reference(f'users/{user_id}')
    user_data = user_ref.get()
    user_name = user_data.get('name', 'User')

    return render_template('dash.html', user_id=user_id, alarms=alarms)


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    # Retrieve the logged-in user's UID from session
    user_id = session.get('user_id')

    if not user_id:
        return redirect(url_for('login'))  # Redirect to login if user is not authenticated

    # Fetch the user's existing profile from Firestore
    user_ref = db.reference('users').child(user_id)
    user_data = user_ref.get()

    if user_data:
        user_data = user_data
    else:
        user_data = None

    # Handle profile update on POST request
    if request.method == 'POST':
        updated_name = request.form.get('name')
        updated_email = request.form.get('email')

        # Validate inputs
        if not updated_name or not updated_email:
            return render_template('profile.html', user=user_data, error="Both fields are required.")

        # Update Firestore document with new values
        user_ref.update({
            'name': updated_name,
            'email': updated_email,
            # Add more fields for other editable information
        })

        # Optionally, you can return a success message here
        return redirect(url_for('profile'))  # Redirect to refresh the page after saving changes

    # Render profile page with current user data
    return render_template('profile.html', user=user_data)


@app.route('/insights', methods=['GET'])
def insights():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    # Get user data from Firebase
    user_ref = db.reference(f'users/{user_id}')
    user_data = user_ref.get()

    if not user_data or 'sessions' not in user_data:
        return "No session data available", 404

    # Check if sessions is a dictionary before attempting to sort
    if isinstance(user_data['sessions'], dict):
        sessions = dict(sorted(user_data['sessions'].items()))  # Ensure chronological order
    else:
        # Handle the case where sessions might not be a dictionary
        return "Invalid session data format", 400

    durations = []
    qualities = []
    daily_data = []

    # Define your quality calculation based on duration
    def calculate_quality(duration):
        if duration >= 420:  # 7 hours in minutes
            return 100
        elif duration >= 360:  # 6 hours in minutes
            return 85
        elif duration >= 300:  # 5 hours in minutes
            return 70
        elif duration >= 240:  # 4 hours in minutes
            return 50
        else:
            return 30  # Less than 4 hours

    for key, session_value in sessions.items():
        duration = session_value.get('duration') if isinstance(session_value, dict) else session_value
        quality = calculate_quality(duration)

        durations.append(duration)
        qualities.append(quality)
        daily_data.append({
            'date': key,
            'duration': duration,
            'quality': quality
        })

    if not durations:
        return "No duration data available", 404

    total_minutes = sum(durations)
    avg_minutes = total_minutes / len(durations)
    max_minutes = max(durations)
    min_minutes = min(durations)

    def format_minutes(mins):
        return f"{int(mins // 60)}h {int(mins % 60)}m"

    summary = {
        'average_duration': format_minutes(avg_minutes),
        'max_duration': format_minutes(max_minutes),
        'min_duration': format_minutes(min_minutes),
        'total_sessions': len(durations),
        'total_sleep': format_minutes(total_minutes),
        'average_quality': round(sum(qualities) / len(qualities), 1) if qualities else 0,
        'daily_data': daily_data,
    }

    def get_change(arr):
        return round(arr[-1] - arr[-2], 1) if len(arr) >= 2 else 0

    trends = {
        'duration_change': get_change(durations),
        'quality_change': get_change(qualities)
    }
    print(qualities)

    recommendations = [
        "Try to go to bed 30 minutes earlier to increase your total sleep time",
        "Consider improving your bedtime routine to boost sleep quality",
        "Avoid caffeine in the evening to help maintain consistent sleep quality"
    ]

    return render_template(
        'insights.html',
        summary=summary,
        daily_data=daily_data,
        trends=trends,
        recommendations=recommendations
    )

# Logout route
@app.route('/logout', methods=['POST'])
def logout():
    global current_user_id
    with user_lock:
        if current_user_id:
            session.pop('user_id', None)
            current_user_id = None
            return jsonify({'status': 'success', 'message': 'Logged out successfully'})
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 400


# API to set the alarm
def create_alarm(user_id, alarm_time):
    try:
        formatted_time = standardize_time_format(alarm_time)
        if not formatted_time:
            return {"status": "error", "message": "Invalid time format"}

        # Get reference to user's alarms
        alarms_ref = db.reference(f'users/{user_id}/alarms')

        # Check for existing active alarms with same time
        existing_alarms = alarms_ref.order_by_child('time').equal_to(formatted_time).get()

        # Check if any existing alarm is active
        active_alarm = next(
            (aid for aid, alarm in (existing_alarms or {}).items()
             if alarm.get('status') == 'active'),
            None
        )

        if active_alarm:
            return {
                "status": "error",
                "message": "Active alarm already exists",
                "alarm_id": active_alarm
            }

        # Create new alarm with Firebase push ID
        new_alarm_ref = alarms_ref.push()
        new_alarm_data = {
            'time': formatted_time,
            'status': 'active',
            'created_at': datetime.utcnow().isoformat()
        }
        new_alarm_ref.set(new_alarm_data)

        # Get the generated alarm ID
        alarm_id = new_alarm_ref.key

        # Set timer logic (keep your existing timer implementation)
        # ...

        return {
            "status": "success",
            "message": "Alarm set",
            "alarm_id": alarm_id,
            "alarm_time": formatted_time
        }

    except Exception as e:
        # Firebase automatically rolls back writes, but we might want to clean up
        if 'new_alarm_ref' in locals():
            new_alarm_ref.delete()
        return {"status": "error", "message": str(e)}


@app.route('/add_alarm', methods=['POST'])
def add_alarm():
    data = request.get_json()
    user_id = data.get('user_id')
    alarm_time = data.get('alarm_time')

    if not user_id or not alarm_time:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400

    try:
        alarm_id = str(uuid.uuid4())
        alarm_data = {
            'time': alarm_time,
            'status': 'active'
        }

        alarms_ref = db.reference(f'users/{user_id}/alarms')
        alarms_ref.child(alarm_id).set(alarm_data)

        return jsonify({'status': 'success', 'alarm': {'time': alarm_time, 'status': 'active'}}), 200

    except Exception as e:
        print(f"Error adding alarm: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

    return jsonify({'status': 'success', 'message': 'Alarm added'})


@app.route('/set_alarm', methods=['POST'])
def set_alarm_route():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    print(f"Received data: {data}")  # Log the incoming data

    if not data or 'time' not in data:
        return jsonify({"error": "Missing time"}), 400

    time = data['time']
    print(f"Received time: {time}")  # Log the time

    # Ensure the time is in the correct format
    formatted_time = standardize_time_format(time)
    if not formatted_time:
        return jsonify({"error": "Invalid time format"}), 400

    # Create the alarm in Firebase
    result = create_alarm(session['user_id'], formatted_time)
    status_code = 201 if result['status'] == 'success' else 400
    return jsonify(result), status_code


# API to cancel an alarm@app.route('/cancel_alarm/<string:alarm_id>', methods=['POST'])
@app.route("/cancel_alarm/<alarm_id>", methods=["POST"])
def cancel_alarm(alarm_id):
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"status": "error", "message": "User not authenticated"}), 403

        alarm_ref = db.reference(f'users/{user_id}/alarms/{alarm_id}')
        alarm_ref.delete()  # Actually remove it from DB to trigger onChildRemoved
        return jsonify({"status": "success", "message": "Alarm cancelled"})

    except Exception as e:
        print(f"Error cancelling alarm: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Function to recognize voice input
def get_voice_command():
    global listening_state, last_command
    recognizer = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            print("Listening for an alarm command...")
            recognizer.adjust_for_ambient_noise(source, duration=1)
            recognizer.energy_threshold = 4000
            recognizer.dynamic_energy_threshold = True
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)

        command = recognizer.recognize_google(audio).lower()
        if command:
            last_command = command
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




def listen_for_wake_word():
    recognizer = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            print("🔍 Waiting for wake word (hey alarm, wake up, or alarm system)...")
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            recognizer.energy_threshold = 3000
            audio = recognizer.listen(source, timeout=3, phrase_time_limit=2)

            transcript = recognizer.recognize_google(audio).lower()
            wake_words = ["hey alarm", "wake up", "alarm system", "stop"]

            if any(wake_word in transcript for wake_word in wake_words):
                return transcript
            return False

    except sr.UnknownValueError:
        return False
    except sr.RequestError:
        print("❌ Speech Recognition service unavailable.")
        return False
    except Exception as e:
        return False

voice_ui_status="idle"
def listen_for_commands():
    global voice_ui_status
    print("Voice recognition system started")
    consecutive_errors = 0
    max_consecutive_errors = 2
    wake_word_detected = False  # Track if wake word was detected

    with app.app_context():
        while True:
            try:
                # 1. PRIORITIZE USER CONNECTION CHECK FIRST
                with user_lock:
                    user_id = current_user_id


                if user_id is None:
                    print("No active user - skipping voice command")
                    voice_ui_status = "idle"
                    time.sleep(2)
                    continue


                # Set alarm commands
                if not wake_word_detected:
                    wake_word_detected = listen_for_wake_word()
                    if wake_word_detected and "stop" in wake_word_detected:
                        print("DETECTED STP")
                        user_ref = db.reference(f'users/{current_user_id}')
                        user_ref.child('hardware/motor').set(0)
                        print("✅ Motor updated to 0.")

                    voice_ui_status = "idle"
                    if not wake_word_detected:
                        time.sleep(0.1)
                        continue
                    print("Wake word detected! Listening for commands...")
                    voice_ui_status = "listening"
                voice_command=get_voice_command()


                if voice_command :
                    print(f"Voice command detected: {voice_command}")
                    consecutive_errors = 0

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
                            response = requests.post(f"http://127.0.0.1:5000/cancel_alarm/{alarm_id}")
                            if response.ok:
                                print(f"✅ Cancelled alarm {alarm_id}")
                            else:
                                print(f"❌ Error: {response.json().get('message', 'Unknown error')}")
                            if result.get('status') == 'success':
                                print(f"✅ Cancelled alarm {alarm_id}")
                            else:
                                print(f"❌ Error: {result.get('message')}")
                        else:
                            print("❌ Could not determine alarm ID. Please specify like 'Cancel alarm 3'")

                    # List alarms command
                    elif "list alarms" in voice_command or "show alarms" in voice_command:
                        try:
                            alarms_ref = db.reference(f'/users/{user_id}/alarms')
                            alarms_data = alarms_ref.get()

                        except Exception as e:
                            print(f"❌ Failed to list alarms: {e}")

                    # Help command
                    elif "help" in voice_command:
                        print("\n🔧 Available Voice Commands:")
                        print("Set alarm [time] - Set a new alarm")
                        print("Cancel alarm [ID] - Cancel an existing alarm")
                        print("List alarms - Show all alarms")
                        print("Help - Show this help message")

                    # Reset wake word detection if user wants to stop
                    elif "stop listening" in voice_command or "go to sleep" in voice_command:
                        wake_word_detected = False
                        print("✅ Returning to wake word detection mode")

                    else:
                        print("⚠️ Command not recognized. Say 'help' for available commands")
                    wake_word_detected = False
                    voice_ui_status="idle"

                else:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        print("⚠️ Too many consecutive errors. Resetting voice recognition...")
                        consecutive_errors = 0
                        wake_word_detected = False  # Reset wake word detection
                        voice_ui_status = "idle"
                        time.sleep(2)

            except Exception as e:
                print(f"⚠️ Unexpected error: {str(e)}")
                consecutive_errors += 1
                wake_word_detected = False  # Reset on error
                voice_ui_status = "idle"
                time.sleep(1)

            time.sleep(0.5)

@app.route('/voice_status')
def get_voice_status():
    global voice_ui_status
    return jsonify({"status":voice_ui_status})


# Main function
def main():
    voice_thread = threading.Thread(target=listen_for_commands, daemon=True)
    voice_thread.start()
    app.run(port=5000, debug=True, use_reloader=False)


if __name__ == "__main__":
    start_scheduler()
    main()


