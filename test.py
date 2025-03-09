import sqlite3

# Connect to the database
conn = sqlite3.connect('voice.db')
c = conn.cursor()

# Add the status column with a default value of 'active'
c.execute('ALTER TABLE alarms ADD COLUMN status TEXT NOT NULL DEFAULT "active"')

# Commit the changes and close the connection
conn.commit()
conn.close()