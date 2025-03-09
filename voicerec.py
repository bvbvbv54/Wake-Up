import speech_recognition as sr
import re
import time
import datetime
import threading
import playsound
from flask import Flask, jsonify, render_template_string, request
from threading import Timer
import sqlite3

# Initialize Flask app
app = Flask(__name__)

# Global variables
active_timers = {}
alarm_lock = threading.Lock()


# Initialize database
def initialize_database():
    conn = sqlite3.connect('voice.db')
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS alarms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'active'
    )
    ''')
    conn.commit()
    conn.close()


# Function to recognize voice input with better error handling
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
    now = datetime.datetime.now()

    # Handle both 24-hour and 12-hour formats
    try:
        if ' ' in alarm_time:  # 12-hour format with AM/PM
            alarm_dt = datetime.datetime.strptime(alarm_time, "%I:%M %p")
        else:  # 24-hour format
            alarm_dt = datetime.datetime.strptime(alarm_time, "%H:%M")
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
            dt = datetime.datetime.strptime(time_str, "%I:%M %p")
        else:  # 24-hour format
            dt = datetime.datetime.strptime(time_str, "%H:%M")

        return dt.strftime("%I:%M %p").lstrip('0')  # Remove leading zero for hour
    except ValueError:
        return time_str  # Return as is if format unknown


# Play alarm sound
def play_alarm():
    print("⏰ Alarm is ringing! Wake up!")
    try:
        playsound.playsound("alarm.mp3")
    except Exception as e:
        print(f"Error playing alarm sound: {e}")
        # Fallback to console notification if sound fails
        for _ in range(5):
            print("\a")  # Console bell
            time.sleep(0.5)


# Save alarm to database
def save_alarm_to_db(alarm_time):
    conn = sqlite3.connect('voice.db')
    c = conn.cursor()
    formatted_time = standardize_time_format(alarm_time)
    c.execute('''
        INSERT INTO alarms (time, status)
        VALUES (?, 'active')
    ''', (formatted_time,))
    alarm_id = c.lastrowid
    conn.commit()
    conn.close()
    return alarm_id


# API to set the alarm
@app.route('/set_alarm', methods=['POST'])
def set_alarm_api():
    data = request.get_json()
    alarm_time = data.get('time')
    if not alarm_time:
        return jsonify({"status": "error", "message": "No time provided"})

    return set_alarm(alarm_time)


# Function to set alarm (used by both API and voice commands)
def set_alarm(alarm_time):
    # Standardize the time format
    formatted_time = standardize_time_format(alarm_time)
    seconds = get_seconds_until_alarm(formatted_time)

    if seconds is None:
        return jsonify({"status": "error", "message": "Invalid time format"})

    # Check for existing active alarm at the same time
    conn = sqlite3.connect('voice.db')
    c = conn.cursor()
    c.execute('''
        SELECT id FROM alarms
        WHERE time = ? AND status = 'active'
    ''', (formatted_time,))
    existing_alarm = c.fetchone()
    conn.close()

    if existing_alarm:
        return jsonify({
            "status": "error",
            "message": "An active alarm already exists at this time",
            "existing_alarm_id": existing_alarm[0]
        })

    # Save the new alarm to the database
    alarm_id = save_alarm_to_db(formatted_time)

    # Start the timer for the new alarm
    def alarm_trigger():
        play_alarm()
        with alarm_lock:
            if alarm_id in active_timers:
                del active_timers[alarm_id]
        # Update the alarm status to "triggered"
        conn = sqlite3.connect('voice.db')
        c = conn.cursor()
        c.execute('''
            UPDATE alarms
            SET status = 'triggered'
            WHERE id = ?
        ''', (alarm_id,))
        conn.commit()
        conn.close()

    with alarm_lock:
        timer = Timer(seconds, alarm_trigger)
        active_timers[alarm_id] = timer
        timer.start()

    return jsonify({"status": "success", "message": "Alarm set", "alarm_id": alarm_id, "alarm_time": formatted_time})


# API to cancel an alarm
@app.route('/cancel_alarm/<int:alarm_id>', methods=['POST'])
def cancel_alarm(alarm_id):
    with alarm_lock:
        # Cancel the timer if it exists
        if alarm_id in active_timers:
            timer = active_timers[alarm_id]
            timer.cancel()
            del active_timers[alarm_id]

        # Update the alarm status in the database
        conn = sqlite3.connect('voice.db')
        c = conn.cursor()
        c.execute('''
            UPDATE alarms
            SET status = 'cancelled'
            WHERE id = ? AND status = 'active'
        ''', (alarm_id,))
        rows_affected = c.rowcount
        conn.commit()
        conn.close()

        if rows_affected > 0:
            return jsonify({"status": "success", "message": "Alarm cancelled", "alarm_id": alarm_id})
        else:
            return jsonify({"status": "error", "message": "Alarm not found or already cancelled", "alarm_id": alarm_id})


# Get all alarms
@app.route('/api/alarms', methods=['GET'])
def get_alarms():
    conn = sqlite3.connect('voice.db')
    c = conn.cursor()
    c.execute("Select id, time FROM alarms WHERE status = 'active' ORDER BY time")
    alarms = c.fetchall()
    conn.close()

    alarm_list = []
    for alarm in alarms:
        alarm_list.append({
            "id": alarm[0],
            "time": alarm[1],
            "created_at": None,
            "status": None
        })

    return jsonify({"alarms": alarm_list})


# Webpage displaying all alarms with improved UI
@app.route('/', methods=['GET'])
def show_alarms():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Voice Alarm System</title>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css" rel="stylesheet">
        <style>
            :root {
                --primary: #4361ee;
                --secondary: #3f37c9;
                --success: #4cc9f0;
                --danger: #f72585;
                --warning: #f8961e;
                --info: #4895ef;
                --dark: #2b2d42;
                --light: #f8f9fa;
                --grey: #adb5bd;
                --card-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                --hover-shadow: 0 8px 16px rgba(0, 0, 0, 0.15);
            }

            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background-color: #f0f2f5;
                color: var(--dark);
                line-height: 1.6;
                min-height: 100vh;
                padding: 20px;
            }

            header {
                text-align: center;
                margin-bottom: 30px;
                padding: 20px 0;
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                color: white;
                border-radius: 12px;
                box-shadow: var(--card-shadow);
            }

            header h1 {
                margin-bottom: 10px;
                font-size: 2.5rem;
            }

            header p {
                font-size: 1.1rem;
                opacity: 0.9;
            }

            .container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 0 20px;
            }

            .clock-container {
                display: flex;
                justify-content: center;
                margin-bottom: 30px;
            }

            .clock {
                font-size: 3rem;
                background-color: var(--dark);
                color: white;
                padding: 20px 40px;
                border-radius: 10px;
                box-shadow: var(--card-shadow);
                display: inline-block;
            }

            .control-panel {
                background-color: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: var(--card-shadow);
                margin-bottom: 30px;
            }

            .form-container {
                display: flex;
                gap: 15px;
                align-items: center;
                flex-wrap: wrap;
            }

            .form-group {
                flex: 1;
                min-width: 200px;
            }

            label {
                display: block;
                margin-bottom: 8px;
                font-weight: 500;
                color: var(--dark);
            }

            input {
                width: 100%;
                padding: 12px 15px;
                border: 1px solid var(--grey);
                border-radius: 6px;
                font-size: 1rem;
                transition: border-color 0.3s, box-shadow 0.3s;
            }

            input:focus {
                border-color: var(--primary);
                box-shadow: 0 0 0 3px rgba(67, 97, 238, 0.2);
                outline: none;
            }

            button {
                cursor: pointer;
                padding: 12px 20px;
                border: none;
                border-radius: 6px;
                font-size: 1rem;
                font-weight: 500;
                transition: transform 0.2s, box-shadow 0.2s, background-color 0.2s;
            }

            button:hover {
                transform: translateY(-2px);
                box-shadow: var(--hover-shadow);
            }

            button:active {
                transform: translateY(0);
            }

            .btn-primary {
                background-color: var(--primary);
                color: white;
            }

            .btn-primary:hover {
                background-color: var(--secondary);
            }

            .btn-danger {
                background-color: var(--danger);
                color: white;
            }

            .btn-danger:hover {
                background-color: #d91a72;
            }

            .btn-action {
                align-self: flex-end;
                margin-top: 28px;
            }

            .alarms-container {
                background-color: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: var(--card-shadow);
            }

            .section-title {
                margin-bottom: 20px;
                font-size: 1.5rem;
                color: var(--dark);
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .section-title i {
                color: var(--primary);
            }

            .alarm-list {
                list-style-type: none;
            }

            .alarm-item {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px;
                margin-bottom: 10px;
                background-color: var(--light);
                border-radius: 8px;
                transition: background-color 0.2s, transform 0.2s;
            }

            .alarm-item:hover {
                background-color: #edf0f5;
                transform: translateX(5px);
            }

            .alarm-info {
                display: flex;
                align-items: center;
                gap: 15px;
            }

            .alarm-time {
                font-size: 1.3rem;
                font-weight: 600;
            }

            .alarm-meta {
                color: var(--grey);
                font-size: 0.9rem;
            }

            .alarm-status {
                padding: 5px 10px;
                border-radius: 20px;
                font-size: 0.85rem;
                font-weight: 600;
            }

            .status-active {
                background-color: rgba(76, 201, 240, 0.2);
                color: var(--info);
            }

            .status-triggered {
                background-color: rgba(72, 149, 239, 0.2);
                color: var(--primary);
            }

            .status-cancelled {
                background-color: rgba(173, 181, 189, 0.2);
                color: var(--grey);
                text-decoration: line-through;
            }

            .no-alarms {
                text-align: center;
                padding: 30px;
                color: var(--grey);
                font-style: italic;
            }

            .icon-bell {
                color: var(--warning);
                font-size: 1.5rem;
                margin-right: 10px;
            }

            .refresh-btn {
                background-color: var(--info);
                color: white;
                border-radius: 50%;
                width: 50px;
                height: 50px;
                display: flex;
                align-items: center;
                justify-content: center;
                position: fixed;
                bottom: 30px;
                right: 30px;
                box-shadow: var(--card-shadow);
                font-size: 1.2rem;
            }

            .refresh-btn:hover {
                background-color: var(--primary);
            }

            .toast {
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 15px 25px;
                background-color: white;
                color: var(--dark);
                border-radius: 8px;
                box-shadow: var(--hover-shadow);
                transform: translateX(150%);
                transition: transform 0.3s ease-out;
                z-index: 1000;
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .toast.show {
                transform: translateX(0);
            }

            .toast-success {
                border-left: 5px solid var(--success);
            }

            .toast-error {
                border-left: 5px solid var(--danger);
            }

            .toast i {
                font-size: 1.2rem;
            }

            .toast i.success {
                color: var(--success);
            }

            .toast i.error {
                color: var(--danger);
            }

            .voice-indicator {
                display: inline-flex;
                align-items: center;
                gap: 10px;
                margin-left: 20px;
                padding: 8px 15px;
                background-color: rgba(255, 255, 255, 0.2);
                border-radius: 20px;
            }

            .voice-status {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background-color: var(--light);
            }

            .voice-status.listening {
                background-color: var(--danger);
                animation: pulse 1.5s infinite;
            }

            @keyframes pulse {
                0% {
                    transform: scale(0.95);
                    box-shadow: 0 0 0 0 rgba(247, 37, 133, 0.7);
                }

                70% {
                    transform: scale(1);
                    box-shadow: 0 0 0 10px rgba(247, 37, 133, 0);
                }

                100% {
                    transform: scale(0.95);
                    box-shadow: 0 0 0 0 rgba(247, 37, 133, 0);
                }
            }

            @media (max-width: 768px) {
                .form-container {
                    flex-direction: column;
                }

                .btn-action {
                    align-self: stretch;
                    margin-top: 15px;
                }

                .alarm-item {
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 10px;
                }

                .alarm-actions {
                    align-self: flex-end;
                }

                .clock {
                    font-size: 2.5rem;
                    padding: 15px 30px;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1><i class="fas fa-clock"></i> Voice Alarm System</h1>
                <p>Set alarms using voice commands or manual input</p>
                <div class="voice-indicator">
                    <div id="voiceStatus" class="voice-status"></div>
                    <span id="voiceText">Voice Recognition</span>
                </div>
            </header>

            <div class="clock-container">
                <div class="clock" id="currentTime">00:00:00</div>
            </div>

            <div class="control-panel">
                <h2 class="section-title"><i class="fas fa-plus-circle"></i> Set New Alarm</h2>
                <div class="form-container">
                    <div class="form-group">
                        <label for="alarmTime">Alarm Time</label>
                        <input type="time" id="alarmTime" required>
                    </div>
                    <div class="btn-action">
                        <button id="setAlarmBtn" class="btn-primary">
                            <i class="fas fa-bell"></i> Set Alarm
                        </button>
                    </div>
                </div>
            </div>

            <div class="alarms-container">
                <h2 class="section-title"><i class="fas fa-list"></i> Your Alarms</h2>
                <ul id="alarmList" class="alarm-list">
                    <!-- Alarms will be loaded here -->
                    <li class="no-alarms">No alarms set yet</li>
                </ul>
            </div>
        </div>

        <button class="refresh-btn" id="refreshBtn">
            <i class="fas fa-sync-alt"></i>
        </button>

        <div id="toast" class="toast">
            <i class="fas fa-check-circle success"></i>
            <span id="toastMessage">Message here</span>
        </div>

        <script>
            // Update the clock
            function updateClock() {
                const now = new Date();
                let hours = now.getHours();
                let minutes = now.getMinutes();
                let seconds = now.getSeconds();

                // Add leading zeros
                hours = hours < 10 ? '0' + hours : hours;
                minutes = minutes < 10 ? '0' + minutes : minutes;
                seconds = seconds < 10 ? '0' + seconds : seconds;

                document.getElementById('currentTime').textContent = `${hours}:${minutes}:${seconds}`;
                setTimeout(updateClock, 1000);
            }

            // Show toast message
            function showToast(message, isError = false) {
                const toast = document.getElementById('toast');
                const toastMessage = document.getElementById('toastMessage');
                const icon = toast.querySelector('i');

                toast.className = isError ? 'toast toast-error show' : 'toast toast-success show';
                toastMessage.textContent = message;

                icon.className = isError ? 'fas fa-exclamation-circle error' : 'fas fa-check-circle success';

                setTimeout(() => {
                    toast.className = 'toast';
                }, 3000);
            }

            // Load alarms from the server
            async function loadAlarms() {
                try {
                    const response = await fetch('/api/alarms');
                    const data = await response.json();

                    const alarmList = document.getElementById('alarmList');
                    alarmList.innerHTML = '';

                    if (data.alarms.length === 0) {
                        alarmList.innerHTML = '<li class="no-alarms">No alarms set yet</li>';
                        return;
                    }

                    data.alarms.forEach(alarm => {
                        const li = document.createElement('li');
                        li.className = 'alarm-item';
                        li.id = `alarm-${alarm.id}`;

                        const statusClass = `status-${alarm.status}`;

                        li.innerHTML = `
                            <div class="alarm-info">
                                <i class="fas fa-bell icon-bell"></i>
                                <div>
                                    <div class="alarm-time">${alarm.time}</div>
                                    <div class="alarm-meta">Alarm ID: ${alarm.id}</div>
                                </div>
                            </div>
                            <div>
                                <span class="alarm-status ${statusClass}">${alarm.status}</span>
                                ${alarm.status === 'active' ? 
                                    `<button onclick="cancelAlarm(${alarm.id})" class="btn-danger" style="margin-left: 10px;">
                                        <i class="fas fa-times"></i> Cancel
                                    </button>` : ''}
                            </div>
                        `;

                        alarmList.appendChild(li);
                    });
                } catch (error) {
                    console.error('Error loading alarms:', error);
                    showToast('Failed to load alarms', true);
                }
            }

            // Set a new alarm
            async function setAlarm() {
                const alarmTimeInput = document.getElementById('alarmTime');
                if (!alarmTimeInput.value) {
                    showToast('Please select a time', true);
                    return;
                }

                try {
                    // Convert to display format (HH:MM)
                    const timeValue = alarmTimeInput.value;

                    const response = await fetch('/set_alarm', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ time: timeValue })
                    });

                    const data = await response.json();

                    if (data.status === 'success') {
                        showToast(`Alarm set for ${data.alarm_time}`);
                        alarmTimeInput.value = '';
                        loadAlarms();
                    } else {
                        showToast(data.message, true);
                    }
                } catch (error) {
                    console.error('Error setting alarm:', error);
                    showToast('Failed to set alarm', true);
                }
            }

            // Cancel an alarm
            async function cancelAlarm(alarmId) {
                try {
                    const response = await fetch(`/cancel_alarm/${alarmId}`, {
                        method: 'POST'
                    });

                    const data = await response.json();

                    if (data.status === 'success') {
                        showToast(`Alarm #${alarmId} cancelled`);
                        loadAlarms();
                    } else {
                        showToast(data.message, true);
                    }
                } catch (error) {
                    console.error('Error cancelling alarm:', error);
                    showToast('Failed to cancel alarm', true);
                }
            }

            // Simulate voice recognition status
            function simulateVoiceActivity() {
                const voiceStatus = document.getElementById('voiceStatus');
                const voiceText = document.getElementById('voiceText');

                // Randomly toggle between listening and standby
                const isListening = Math.random() > 0.7;

                if (isListening) {
                    voiceStatus.className = 'voice-status listening';
                    voiceText.textContent = 'Listening...';
                } else {
                    voiceStatus.className = 'voice-status';
                    voiceText.textContent = 'Voice Recognition';
                }

                setTimeout(simulateVoiceActivity, isListening ? 3000 : 1000);
            }

            // Initialize
            document.addEventListener('DOMContentLoaded', () => {
                updateClock();
                loadAlarms();
                simulateVoiceActivity();

                document.getElementById('setAlarmBtn').addEventListener('click', setAlarm);
                document.getElementById('refreshBtn').addEventListener('click', loadAlarms);
            });
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)


# Listen for voice commands
def listen_for_commands():
    print("Voice recognition system started")
    consecutive_errors = 0
    max_consecutive_errors = 5

    # Create application context for the thread
    with app.app_context():
        while True:
            try:
                voice_command = get_voice_command()

                if voice_command:
                    print(f"Voice command detected: {voice_command}")
                    consecutive_errors = 0  # Reset error counter

                    # Set alarm commands
                    if any(phrase in voice_command for phrase in ["set the alarm", "set an alarm", "set alarm"]):
                        alarm_time = extract_time(voice_command)
                        if alarm_time:
                            print(f"✅ Setting alarm for {alarm_time}")
                            with app.test_request_context():  # Create mock request context
                                result = set_alarm(alarm_time)
                                if 'error' in result.get_json()['status']:
                                    print(f"❌ Error: {result.get_json()['message']}")
                        else:
                            print("❌ Could not extract time. Please include a specific time.")

                    # Cancel alarm commands
                    elif "cancel alarm" in voice_command or "delete alarm" in voice_command:
                        match = re.search(
                            r'(?:cancel|delete)\s+alarm\s+(?:number\s+|id\s+|#)?(\d+)',
                            voice_command,
                            re.IGNORECASE
                        )
                        print (match)
                        if match:
                            alarm_id = int(match.group(1))
                            with app.test_request_context():  # Create mock request context
                                result = cancel_alarm(alarm_id)
                                if result.get_json()['status'] == 'success':
                                    print(f"❌ Cancelled alarm {alarm_id}")
                                else:
                                    print(f"❌ Error: {result.get_json()['message']}")
                        else:
                            print("❌ Could not determine alarm ID. Please specify like 'Cancel alarm 3'")

                    # List alarms command
                    elif "list alarms" in voice_command or "show alarms" in voice_command:
                        with app.app_context():  # Database operations context
                            conn = sqlite3.connect('voice.db')
                            c = conn.cursor()
                            c.execute("SELECT id, time, status FROM alarms ORDER BY time")
                            alarms = c.fetchall()
                            conn.close()

                            if alarms:
                                print("\n📋 Current Alarms:")
                                for alarm in alarms:
                                    print(f"ID: {alarm[0]} | Time: {alarm[1]} | Status: {alarm[2]}")
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
    initialize_database()
    voice_thread = threading.Thread(target=listen_for_commands, daemon=True)
    voice_thread.start()
    app.run(port=5000, debug=True, use_reloader=False)

if __name__ == "__main__":
    main()