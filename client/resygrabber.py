import click
import inquirer
import json
import random
import os
import time
import capsolver
import re
import uuid
import urllib.parse
import requests
from datetime import datetime
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore
import threading
import time
from task_executor import run_tasks_concurrently

scheduler = BackgroundScheduler()
scheduler.add_jobstore(MemoryJobStore(), 'default')
scheduler.start()

# Thread synchronization locks
_file_lock = threading.Lock()  # Lock for all file I/O operations
_running_tasks_lock = threading.Lock()  # Lock for running_tasks dictionary

running_tasks = {}

# File paths for storing data
TASKS_FILE = 'tasks.json'
PROXIES_FILE = 'proxies.json'
INFO_FILE = 'info.json'
ACCESS_KEY_FILE = 'access_key.json'
ACCOUNTS_FILE = 'accounts.json'
RESERVATIONS_FILE = 'resrevations.json'
SCHEDULED_TASKS_FILE = 'scheduled_tasks.json'
RESTAURANT_CACHE_FILE = 'restaurant_cache.json'

# Load existing data (thread-safe)
def load_data(file, default):
    with _file_lock:
        if os.path.exists(file):
            try:
                with open(file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return default
        return default

# Save data to file (thread-safe with atomic write)
def save_data(file, data):
    with _file_lock:
        # Write to temp file first, then rename for atomicity
        temp_file = file + '.tmp'
        try:
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=4)
            os.replace(temp_file, file)  # Atomic on POSIX systems
        except Exception:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            raise

# Atomic read-modify-write operation for file data
def update_data(file, default, update_fn):
    """
    Atomically load data, apply update_fn, and save.
    update_fn receives the current data and should return the modified data.
    """
    with _file_lock:
        if os.path.exists(file):
            try:
                with open(file, 'r') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = default
        else:
            data = default

        data = update_fn(data)

        temp_file = file + '.tmp'
        try:
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=4)
            os.replace(temp_file, file)
        except Exception:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            raise

        return data

def check_token_validity(auth_token):
    """Check if an auth token is still valid by making a lightweight API call"""
    try:
        headers = {
            'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
            'X-Resy-Auth-Token': auth_token,
            'X-Resy-Universal-Auth': auth_token,
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Origin': 'https://resy.com',
            'Referer': 'https://resy.com/',
        }
        r = requests.get('https://api.resy.com/2/user', headers=headers, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def get_restaurant_name(restaurant_id):
    """Get restaurant name from cache or fetch from API (thread-safe)"""
    # First check cache with lock
    cache = load_data(RESTAURANT_CACHE_FILE, {})
    if restaurant_id in cache:
        return cache[restaurant_id]

    # Fetch from API (without holding lock to avoid blocking other threads)
    name = None
    try:
        headers = {
            'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Origin': 'https://resy.com',
            'Referer': 'https://resy.com/',
        }
        r = requests.get(f'https://api.resy.com/3/venue?id={restaurant_id}', headers=headers)
        if r.status_code == 200:
            name = r.json().get('name', f'Restaurant {restaurant_id}')
    except Exception as e:
        print(f"Could not fetch restaurant name for {restaurant_id}: {e}")

    if name is None:
        return f"Restaurant {restaurant_id}"

    # Atomically update cache (re-check in case another thread added it)
    def update_cache(cache):
        if restaurant_id not in cache:
            cache[restaurant_id] = name
        return cache

    update_data(RESTAURANT_CACHE_FILE, {}, update_cache)
    return name

def send_task_reminders():
    """Send Discord summary of all tasks on bot startup"""
    tasks = load_data(TASKS_FILE, [])
    info = load_data(INFO_FILE, {})
    accounts = load_data(ACCOUNTS_FILE, [])
    proxies = load_data(PROXIES_FILE, [])
    webhook_url = info.get('discord_webhook', '')

    if not webhook_url or webhook_url == 'none':
        return

    today = datetime.date.today().strftime('%Y-%m-%d')
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %I:%M %p')

    lines = [f"**ResyBot Started** | {current_time}\n"]

    # System status section
    lines.append("**System Status:**")

    # Check account tokens
    if accounts:
        valid_count = 0
        invalid_accounts = []
        for account in accounts:
            if check_token_validity(account.get('auth_token', '')):
                valid_count += 1
            else:
                invalid_accounts.append(account.get('account_name', 'Unknown'))

        if valid_count == len(accounts):
            lines.append(f"• Accounts: {len(accounts)} configured, all tokens valid")
        else:
            lines.append(f"• Accounts: {len(accounts)} configured, {valid_count} valid, {len(accounts) - valid_count} expired")
            if invalid_accounts:
                lines.append(f"  ⚠️ Expired: {', '.join(invalid_accounts)}")
    else:
        lines.append("• Accounts: None configured")

    # Proxy status
    lines.append(f"• Proxies: {len(proxies)} loaded" if proxies else "• Proxies: None configured")

    # Captcha service status
    captcha_services = []
    if info.get('capsolver_key'):
        captcha_services.append("CAPSolver")
    if info.get('capmonster_key'):
        captcha_services.append("CapMonster")
    lines.append(f"• Captcha: {', '.join(captcha_services)}" if captcha_services else "• Captcha: Not configured")

    lines.append("")

    if not tasks:
        lines.append("**Active Tasks:** None")
    else:
        lines.append("**Active Tasks:**")

    for i, task in enumerate(tasks):
        start_date = task.get('start_date', 'N/A')
        task_name = task.get('task_name', f"Restaurant {task['restaurant_id']}")

        # Add urgency indicator
        if start_date == today:
            urgency = "[TODAY]"
        elif start_date == tomorrow:
            urgency = "[TOMORROW]"
        else:
            urgency = ""

        # Convert 24h to 12h format
        start_hour = task.get('start_time', 0)
        end_hour = task.get('end_time', 0)
        start_12h = f"{start_hour % 12 or 12} {'AM' if start_hour < 12 else 'PM'}"
        end_12h = f"{end_hour % 12 or 12} {'AM' if end_hour < 12 else 'PM'}"

        lines.append(f"• **{task_name}** | {task['party_sz']} guests | {start_date} to {task.get('end_date', start_date)} | {start_12h} - {end_12h} {urgency}")

    lines.append("")

    # Add scheduled tasks section
    scheduled_tasks_data = load_data(SCHEDULED_TASKS_FILE, [])
    if scheduled_tasks_data:
        lines.append("**Scheduled Runs:**")
        for st in scheduled_tasks_data:
            task_index = st['task_index']
            if task_index < len(tasks):
                task_name = tasks[task_index].get('task_name', f"Restaurant {tasks[task_index]['restaurant_id']}")
            else:
                task_name = "Unknown Task"

            schedule_date = st.get('schedule_date', '')
            schedule_time = st.get('schedule_time', '')
            repeat = st.get('repeat', 'Once')

            # Convert time to 12h format
            try:
                hour = int(schedule_time.split(':')[0])
                time_12h = f"{hour % 12 or 12}:{schedule_time.split(':')[1]} {'AM' if hour < 12 else 'PM'}"
            except:
                time_12h = schedule_time

            if repeat == 'Once' and schedule_date:
                lines.append(f"• {task_name} | {schedule_date} at {time_12h}")
            else:
                lines.append(f"• {task_name} | {repeat} at {time_12h}")
        lines.append("")

    message = "\n".join(lines)

    try:
        requests.post(webhook_url, json={"content": message})
        print(f"Sent startup summary for {len(tasks)} task(s) to Discord")
    except Exception as e:
        print(f"Failed to send Discord summary: {e}")

# Send reminders on startup
send_task_reminders()

# Save scheduled task to file for persistence (thread-safe)
def save_scheduled_task(job_id, task_index, schedule_time, repeat, duration, schedule_date=""):
    def add_task(scheduled_tasks):
        scheduled_tasks.append({
            'job_id': job_id,
            'task_index': task_index,
            'schedule_time': schedule_time,
            'schedule_date': schedule_date,
            'repeat': repeat,
            'duration': duration
        })
        return scheduled_tasks

    update_data(SCHEDULED_TASKS_FILE, [], add_task)

# Remove scheduled task from file (thread-safe)
def remove_scheduled_task_from_file(job_id):
    def remove_task(scheduled_tasks):
        return [t for t in scheduled_tasks if t['job_id'] != job_id]

    update_data(SCHEDULED_TASKS_FILE, [], remove_task)

# Reload scheduled tasks on startup
def reload_scheduled_tasks():
    scheduled_tasks = load_data(SCHEDULED_TASKS_FILE, [])
    for task_data in scheduled_tasks:
        try:
            schedule_time = datetime.datetime.strptime(task_data['schedule_time'], "%H:%M").time()
            job_id = task_data['job_id']
            task_index = task_data['task_index']
            duration = task_data['duration']
            repeat = task_data['repeat']
            schedule_date_str = task_data.get('schedule_date', "")

            if repeat == 'Daily':
                scheduler.add_job(start_and_stop_task, 'cron', args=[task_index, duration, job_id],
                                  hour=schedule_time.hour, minute=schedule_time.minute, id=job_id)
            elif repeat == 'Weekly':
                scheduler.add_job(start_and_stop_task, 'cron', args=[task_index, duration, job_id],
                                  day_of_week='mon-sun', hour=schedule_time.hour, minute=schedule_time.minute, id=job_id)
            else:  # Once
                if schedule_date_str:
                    schedule_date = datetime.datetime.strptime(schedule_date_str, "%Y-%m-%d").date()
                else:
                    schedule_date = datetime.date.today()
                next_run = datetime.datetime.combine(schedule_date, schedule_time)
                # Skip if already in the past
                if next_run <= datetime.datetime.now():
                    print(f"Skipping past scheduled task: {job_id}")
                    remove_scheduled_task_from_file(job_id)
                    continue
                scheduler.add_job(start_and_stop_task, 'date', args=[task_index, duration, job_id],
                                  run_date=next_run, id=job_id)
            print(f"Reloaded scheduled task: {job_id}")
        except Exception as e:
            print(f"Failed to reload scheduled task {task_data.get('job_id', 'unknown')}: {e}")

# Main CLI
@click.group()
def cli():
    pass

# Main Menu
@cli.command()
def menu():
    while True:
        click.clear()
        click.echo(click.style('ResyGrabber', bold=True, fg='cyan', bg='black'))
        questions = [
            inquirer.List('choice',
                          message="Choose an option",
                          choices=['1) Show tasks', '2) Proxies', '3) Info', '4) Manage Accounts', '5) Generate Accounts', '6) View Reservations', '7) Start Tasks', '8) Schedule Tasks', '9) Manage Scheduled tasks', 'Exit'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)

        if answers['choice'].startswith('1'):
            show_tasks()
        elif answers['choice'].startswith('2'):
            manage_proxies()
        elif answers['choice'].startswith('3'):
            manage_info()
        elif answers['choice'].startswith('4'):
            manage_accounts()
        elif answers['choice'].startswith('5'):
            generate_accounts()
        elif answers['choice'].startswith('6'):
            list_reservations()
        elif answers['choice'].startswith('7'):
            print("Tasks are running...")
            try:
                start_tasks()
            except Exception as e:
                print(f"Error starting tasks: {e}")
        elif answers['choice'].startswith('8'):
            schedule_tasks()
        elif answers['choice'].startswith('9'):
            view_scheduled_tasks()
        elif answers['choice'] == 'Exit':
            break

import atexit
atexit.register(lambda: scheduler.shutdown())

# Show Tasks
def show_tasks():
    while True:
        tasks = load_data(TASKS_FILE, [])
        click.clear()
        click.echo(click.style('Tasks', bold=True, fg='cyan'))
        for idx, task in enumerate(tasks):
            click.echo(f'{idx + 1}) {task}')
        questions = [
            inquirer.List('task_choice',
                          message="Choose an option",
                          choices=['a) Add task', 'd) Delete task', 'Back'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)

        if answers['task_choice'] == 'a) Add task':
            add_task()
        elif answers['task_choice'] == 'd) Delete task':
            delete_task(tasks)
        elif answers['task_choice'] == 'Back':
            break

""" def add_task():
    info = load_data(INFO_FILE, {})
    accounts = load_data(ACCOUNTS_FILE, [])
    captcha_services = []
    if 'capsolver_key' in info:
        captcha_services.append('CAPSolver')
    if 'capmonster_key' in info:
        captcha_services.append('CapMonster')

    if not accounts:
        click.echo('No accounts found. Please add accounts before adding tasks.')
        return

    account_choices = [
        (f'{idx + 1}) Account Name: {account["account_name"]}', idx)
        for idx, account in enumerate(accounts)
    ]

    questions = [
        inquirer.List('account_choice', message="Select an account for this task:", choices=account_choices),
        inquirer.Text('restaurant_id', message="Please enter the restaurant ID:"),
        inquirer.Text('party_sz', message="Please enter the party sizes (comma-separated, e.g., 2,3,4):"),
        inquirer.Text('start_date', message="Please enter the start date (YYYY-MM-DD):"),
        inquirer.Text('end_date', message="Please enter the end date (YYYY-MM-DD):"),
        inquirer.Text('start_time', message="Please enter the start time (Hour only, 0-23):"),
        inquirer.Text('end_time', message="Please enter the end time (Hour only, 0-23):"),
    ]
    if captcha_services:
        questions.append(inquirer.List('captcha_service',
                                       message="Select the CAPTCHA solving service:",
                                       choices=captcha_services))
    questions.append(inquirer.Text('delay', message="Enter the delay in milliseconds:"))
    questions.append(inquirer.Confirm('save_task', message="Do you want to save these tasks?", default=True))

    answers = inquirer.prompt(questions)

    if answers['save_task']:
        selected_account_index = answers['account_choice']
        selected_account = accounts[selected_account_index]
        
        # Parse multiple party sizes
        party_sizes = [int(size.strip()) for size in answers['party_sz'].split(',') if size.strip().isdigit()]

        tasks = load_data(TASKS_FILE, [])

        for party_size in party_sizes:
            task = {
                'account_name': selected_account['account_name'],
                'auth_token': selected_account['auth_token'],
                'payment_id': selected_account['payment_id'],
                'restaurant_id': answers['restaurant_id'],
                'party_sz': party_size,
                'start_date': answers['start_date'],
                'end_date': answers['end_date'],
                'start_time': int(answers['start_time']),
                'end_time': int(answers['end_time']),
                'captcha_service': answers.get('captcha_service'),
                'delay': int(answers['delay'])
            }
            tasks.append(task)

        save_data(TASKS_FILE, tasks)
    else:
        click.echo('Tasks not saved.')
 """

def add_task():
    info = load_data(INFO_FILE, {})
    accounts = load_data(ACCOUNTS_FILE, [])
    captcha_services = []
    if 'capsolver_key' in info:
        captcha_services.append('CAPSolver')
    if 'capmonster_key' in info:
        captcha_services.append('CapMonster')

    if not accounts:
        click.echo('No accounts found. Please add accounts before adding tasks.')
        return

    account_choices = [
        inquirer.Checkbox('selected_accounts',
                          message="Select accounts for this task (use spacebar to select, enter to confirm):",
                          choices=[(account['account_name'], idx) for idx, account in enumerate(accounts)])
    ]

    # First, prompt for account selection
    account_answers = inquirer.prompt(account_choices)
    
    if not account_answers['selected_accounts']:
        click.echo('No accounts selected. Task creation cancelled.')
        return

    # First get restaurant ID so we can fetch the name
    restaurant_question = [
        inquirer.Text('restaurant_id', message="Please enter the restaurant ID:")
    ]
    restaurant_answer = inquirer.prompt(restaurant_question)
    restaurant_id = restaurant_answer['restaurant_id']

    # Get restaurant name from cache or API
    restaurant_name = get_restaurant_name(restaurant_id)
    click.echo(f'Found restaurant: {restaurant_name}')

    # Then, prompt for other task details
    task_questions = [
        inquirer.Text('task_name', message=f"Enter a name for this task:", default=restaurant_name),
        inquirer.Text('party_sz', message="Please enter the party sizes (comma-separated, e.g., 2,3,4):"),
        inquirer.Text('start_date', message="Please enter the start date (YYYY-MM-DD):"),
        inquirer.Text('end_date', message="Please enter the end date (YYYY-MM-DD):"),
        inquirer.Text('start_time', message="Please enter the start time (Hour only, 0-23):"),
        inquirer.Text('end_time', message="Please enter the end time (Hour only, 0-23):"),
    ]
    if captcha_services:
        task_questions.append(inquirer.List('captcha_service',
                                            message="Select the CAPTCHA solving service:",
                                            choices=captcha_services))
    task_questions.append(inquirer.Text('delay', message="Enter the delay in milliseconds:"))
    task_questions.append(inquirer.Confirm('save_task', message="Do you want to save these tasks?", default=True))

    task_answers = inquirer.prompt(task_questions)

    if task_answers['save_task']:
        selected_account_indices = account_answers['selected_accounts']

        # Parse multiple party sizes
        party_sizes = [int(size.strip()) for size in task_answers['party_sz'].split(',') if size.strip().isdigit()]

        tasks = load_data(TASKS_FILE, [])

        for selected_account_index in selected_account_indices:
            selected_account = accounts[selected_account_index]
            for party_size in party_sizes:
                task = {
                    'task_name': task_answers['task_name'],
                    'account_name': selected_account['account_name'],
                    'auth_token': selected_account['auth_token'],
                    'payment_id': selected_account['payment_id'],
                    'restaurant_id': restaurant_id,
                    'party_sz': party_size,
                    'start_date': task_answers['start_date'],
                    'end_date': task_answers['end_date'],
                    'start_time': int(task_answers['start_time']),
                    'end_time': int(task_answers['end_time']),
                    'captcha_service': task_answers.get('captcha_service'),
                    'delay': int(task_answers['delay'])
                }
                tasks.append(task)

        save_data(TASKS_FILE, tasks)
        click.echo(f'Tasks saved for {len(selected_account_indices)} accounts.')
    else:
        click.echo('Tasks not saved.')


def delete_task(tasks):
    # Filter out non-dictionary tasks
    valid_tasks = [task for task in tasks if isinstance(task, dict)]
    
    task_choices = [f'{idx + 1}) {task["restaurant_id"]}, {task["start_date"]}-{task["end_date"]}, {task["start_time"]}-{task["end_time"]}, Delay: {task["delay"]}ms' for idx, task in enumerate(valid_tasks)]
    questions = [
        inquirer.List('task_to_delete',
                      message="Select a task to delete",
                      choices=task_choices + ['Cancel'])
    ]
    answers = inquirer.prompt(questions)
    if answers['task_to_delete'] != 'Cancel':
        task_index = int(answers['task_to_delete'].split(')')[0]) - 1
        valid_tasks.pop(task_index)
        save_data(TASKS_FILE, valid_tasks)
        click.echo('Task deleted!')

# Manage Proxies
def manage_proxies():
    while True:
        proxies = load_data(PROXIES_FILE, [])
        click.clear()
        click.echo(click.style('Proxies', bold=True, fg='cyan'))
        for idx, proxy in enumerate(proxies):
            click.echo(f'{idx + 1}) {proxy}')
        questions = [
            inquirer.List('proxy_choice',
                          message="Choose an option",
                          choices=['a) Add proxy', 'b) Delete proxy', 'c) Delete all proxies', 'Back'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)

        if answers['proxy_choice'] == 'a) Add proxy':
            add_proxy()
        elif answers['proxy_choice'] == 'b) Delete proxy':
            delete_proxy()
        elif answers['proxy_choice'] == 'c) Delete all proxies':
            delete_all_proxies()
        elif answers['proxy_choice'] == 'Back':
            break

def add_proxy():
    questions = [
        inquirer.Text('proxies', message="Enter the proxies (separated by commas):")
    ]
    answers = inquirer.prompt(questions)
    proxies = load_data(PROXIES_FILE, [])  # Reload proxies to get the latest list
    new_proxies = [proxy.strip() for proxy in answers['proxies'].split(',')]
    proxies.extend(new_proxies)
    save_data(PROXIES_FILE, proxies)
    click.echo('Proxies added!')

def delete_proxy():
    while True:
        proxies = load_data(PROXIES_FILE, [])  # Reload proxies to get the latest list
        proxy_choices = [f'{idx + 1}) {proxy}' for idx, proxy in enumerate(proxies)]
        questions = [
            inquirer.List('proxy_to_delete',
                          message="Select a proxy to delete",
                          choices=proxy_choices + ['Cancel'])
        ]
        answers = inquirer.prompt(questions)
        if answers['proxy_to_delete'] != 'Cancel':
            proxy_index = int(answers['proxy_to_delete'].split(')')[0]) - 1
            proxies.pop(proxy_index)
            save_data(PROXIES_FILE, proxies)
            click.echo('Proxy deleted!')
        if answers['proxy_to_delete'] == 'Cancel':
            break

def delete_all_proxies():
    questions = [
        inquirer.Confirm('confirm_delete_all', message="Are you sure you want to delete all proxies?", default=False)
    ]
    answers = inquirer.prompt(questions)
    if answers['confirm_delete_all']:
        save_data(PROXIES_FILE, [])
        click.echo('All proxies deleted!')
    else:
        click.echo('No proxies were deleted.')

# Manage User Info
def manage_info():
    info = load_data(INFO_FILE, {})
    while True:
        click.clear()
        click.echo(click.style('User Info', bold=True, fg='cyan'))
        # click.echo(f'Payment ID: {info.get("payment_id", "Not set")}')
        # click.echo(f'Auth Token: {info.get("auth_token", "Not set")}')
        click.echo(f'CAPSolver Key: {info.get("capsolver_key", "Not set")}')
        click.echo(f'CapMonster Key: {info.get("capmonster_key", "Not set")}')
        click.echo(f'Discord Webhook: {info.get("discord_webhook", "Not set")}')
        questions = [
            inquirer.List('info_choice',
                          message="Choose an option",
                          choices=['Set CAPSolver Key', 'Set CapMonster Key', 'Set Discord Webhook', 'Back'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)

        # if answers['info_choice'] == 'Set Payment ID':
        #     set_payment_id(info)
        # elif answers['info_choice'] == 'Set Auth Token':
        #     set_auth_token(info)
        if answers['info_choice'] == 'Set CAPSolver Key':
            set_capsolver_key(info)
        elif answers['info_choice'] == 'Set CapMonster Key':
            set_capmonster_key(info)
        elif answers['info_choice'] == 'Set Discord Webhook':
            set_discord_webhook(info)
        elif answers['info_choice'] == 'Back':
            break

def set_payment_id(info):
    questions = [
        inquirer.Text('payment_id', message="Enter your Resy Payment ID")
    ]
    answers = inquirer.prompt(questions)
    info['payment_id'] = answers['payment_id']
    save_data(INFO_FILE, info)
    click.echo('Payment ID set!')

def set_auth_token(info):
    auth_token = input("Enter your Resy Auth Token: ").strip()
    sanitized_auth_token = re.sub(r'\s+', '', auth_token)
    info['auth_token'] = sanitized_auth_token
    save_data(INFO_FILE, info)
    click.echo('Auth Token set!')

def set_capsolver_key(info):
    questions = [
        inquirer.Text('capsolver_key', message="Enter your CAPSolver Key")
    ]
    answers = inquirer.prompt(questions)
    info['capsolver_key'] = answers['capsolver_key']
    save_data(INFO_FILE, info)
    click.echo('CAPSolver Key set!')

def set_capmonster_key(info):
    questions = [
        inquirer.Text('capmonster_key', message="Enter your CapMonster Key")
    ]
    answers = inquirer.prompt(questions)
    info['capmonster_key'] = answers['capmonster_key']
    save_data(INFO_FILE, info)
    click.echo('CapMonster Key set!')

def set_discord_webhook(info):
    discord_webhook = input("Enter your Discord Webhook URL: ").strip()
    sanitized_webhook = re.sub(r'\s+', '', discord_webhook)
    info['discord_webhook'] = sanitized_webhook
    save_data(INFO_FILE, info)
    click.echo('Discord Webhook URL set!')

def manage_accounts():
    while True:
        accounts = load_data(ACCOUNTS_FILE, [])
        click.clear()
        click.echo(click.style('Accounts', bold=True, fg='cyan'))
        for idx, account in enumerate(accounts):
            click.echo(f'{idx + 1}) {account}')
        questions = [
            inquirer.List('account_choice',
                          message="Choose an option",
                          choices=['a) Add account', 'b) Delete account', 'Back'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)

        if answers['account_choice'] == 'a) Add account':
            add_account()
        elif answers['account_choice'] == 'b) Delete account':
            delete_account()
        elif answers['account_choice'] == 'Back':
            break

def add_account():
    accounts = load_data(ACCOUNTS_FILE, [])
    
    auth_token = input("Enter your Resy Auth Token: ").strip()
    payment_id = input("Enter your Resy Payment ID: ").strip()
    account_name = input("Enter a name for this account: ").strip()
    
    account = {
        'auth_token': auth_token,
        'payment_id': payment_id,
        'account_name': account_name
    }
    accounts.append(account)
    save_data(ACCOUNTS_FILE, accounts)
    click.echo('Account added!')

def delete_account():
    accounts = load_data(ACCOUNTS_FILE, [])
    if not accounts:
        click.echo('No accounts found.')
        return
    account_choices = [f'{idx + 1}) Account Name: {account["account_name"]}' for idx, account in enumerate(accounts)]
    questions = [
        inquirer.List('account_to_delete',
                      message="Select an account to delete",
                      choices=account_choices + ['Cancel'])
    ]
    answers = inquirer.prompt(questions)
    if answers['account_to_delete'] != 'Cancel':
        account_index = int(answers['account_to_delete'].split(')')[0]) - 1
        accounts.pop(account_index)
        save_data(ACCOUNTS_FILE, accounts)
        click.echo('Account deleted!')

def get_random_proxy():
    proxies = load_data(PROXIES_FILE, [])
    proxy = random.choice(proxies)
    ip_port, user_pass = proxy.rsplit(':', 2)[0], proxy.rsplit(':', 2)[1:]
    proxiesObj = {
        'http': f'http://{user_pass[0]}:{user_pass[1]}@{ip_port}',
        'https': f'http://{user_pass[0]}:{user_pass[1]}@{ip_port}'
    }
    return proxiesObj

def generate_accounts():
    accounts = load_data(ACCOUNTS_FILE, [])
    info = load_data(INFO_FILE, {})
    proxies = load_data(PROXIES_FILE, [])

    if(info['capsolver_key'] == ''):
        click.echo('Please set your CAPSolver Key before generating accounts.')
        return

    if not proxies:
        click.echo('No proxies found. Please add proxies before generating accounts.')
        return
    
    questions = [
        inquirer.Text('first_name', message="Enter the first name"),
        inquirer.Text('last_name', message="Enter the last name"),
        inquirer.Text('mobile_number', message="Enter the phone number (ex: 2145557505)"),
        inquirer.Text('em_address', message="Enter the email"),
        inquirer.Text('password', message="Enter the password"),
        #inquirer.Text('card_number', message="Enter the card number"),
        #inquirer.Text('exp_month', message="Enter the expiration month (2 digits, ex: 02)"),
        #inquirer.Text('exp_year', message="Enter the expiration year (2 digits, ex: 24)"),
        #inquirer.Text('cvv', message="Enter the CVV (3 digits)"),
        inquirer.Text('zip_code', message="Enter the zip code"),
        inquirer.Text('acc_name', message="Enter the account name"),
    ]

    answers = inquirer.prompt(questions)
    proxy = random.choice(proxies)
    capToken = get_captcha_token(info['capsolver_key'], '6Lfw-dIZAAAAAESRBH4JwdgfTXj5LlS1ewlvvCYe', 'https://resy.com', proxy)
    new_device_token = str(uuid.uuid4())
    print(f'CAPTCHA Token: {capToken}\n')
    data = {
        'first_name': answers['first_name'],
        'last_name': answers['last_name'],
        'mobile_number': f'+1{answers["mobile_number"]}',
        'em_address': answers['em_address'],
        'policies_accept': 1,
        'complete': 1,
        'device_type_id': 3,
        'device_token': new_device_token,
        'marketing_opt_in': 0,
        'isNonUS': 0,
        'password': answers['password'],
        'captcha_token': capToken,
    }

    # paymentdata = {
    #     'card_number': answers['card_number'],
    #     'exp_month': answers['exp_month'],
    #     'exp_year': answers['exp_year'],
    #     'cvv': answers['cvv'],
    #     'zip_code': answers['zip_code'],
    # }

    headers = {
        'Host': 'api.resy.com',
        'X-Origin': 'https://resy.com',
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Cache-Control': 'no-cache',
        'Sec-Fetch-Dest': 'empty',
        'Referer': 'https://resy.com/',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    ip_port, user_pass = proxy.rsplit(':', 2)[0], proxy.rsplit(':', 2)[1:]
    proxiesObj = {
        'http': f'http://{user_pass[0]}:{user_pass[1]}@{ip_port}',
        'https': f'http://{user_pass[0]}:{user_pass[1]}@{ip_port}'
    }

    res = requests.post('https://api.resy.com/2/user/registration', headers=headers, data=data, proxies=proxiesObj, verify=False)
    response = res.json()
    accToken = response['user']['token']
    print(f'Account Token: {accToken}\n')
    #Sleep for 2 seconds
    time.sleep(2)

    # Create new account entry
    new_account = {
        'account_name': answers['acc_name'],
        'auth_token': accToken,
        'payment_id': '0'  # Placeholder payment ID
    }

    # Add the new account to the accounts list
    accounts.append(new_account)

    # Save updated accounts list to file
    save_data(ACCOUNTS_FILE, accounts)

    print(f'Account "{answers["acc_name"]}" has been saved successfully!')

    #client_secret = setup_intent(accToken, proxiesObj)
    #pm = getPm(paymentdata, client_secret, proxiesObj)
    #setPm(accToken, pm, proxiesObj)

    #time.sleep(15)

def get_captcha_token(captcha_key, site_key, url, proxy):
    #put http:// in front of the proxy
    proxy = 'http://' + proxy
    capsolver.api_key = captcha_key
    PAGE_URL = url
    PAGE_KEY = site_key
    print(f'Proxy: {proxy}')
    print('Solving CAPTCHA...')
    return solve_recaptcha_v2(PAGE_URL, PAGE_KEY, proxy)

def solve_recaptcha_v2(url, key, proxy):
    while True:
        try:
            solution = capsolver.solve({
                "type": "RecaptchaV2Task",
                "websiteURL": url,
                "websiteKey": key,
                "proxy": proxy
            })

            if 'gRecaptchaResponse' in solution:
                return solution['gRecaptchaResponse']
            else:
                # Handle cases where the solution does not include the expected key
                print("CAPTCHA solving is still in progress or there was an error.")
                time.sleep(3)  # Wait before trying again

        except Exception as e:
            print(f"An error occurred: {e}")
            time.sleep(5)  # Wait before trying again

def setup_intent(accToken, proxiesObj):
    headers = {
        'Host': 'api.resy.com',
        'Accept': 'application/json, text/plain, */*',
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'Sec-Fetch-Site': 'same-site',
        'X-Origin': 'https://resy.com',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'X-Resy-Auth-Token': accToken,
        'Sec-Fetch-Mode': 'cors',
        'Origin': 'https://resy.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Referer': 'https://resy.com/',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'X-Resy-Universal-Auth': accToken,
    }

    response = requests.post('https://api.resy.com/3/stripe/setup_intent', headers=headers, proxies=proxiesObj, verify=False)
    res = response.json()
    return res['client_secret']

def setPm(accToken, pm, proxiesObj):
    headers = {
        'Host': 'api.resy.com',
        'Accept': 'application/json, text/plain, */*',
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'Sec-Fetch-Site': 'same-site',
        'X-Origin': 'https://resy.com',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'Sec-Fetch-Mode': 'cors',
        'X-Resy-Auth-Token': accToken,
        'Origin': 'https://resy.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Referer': 'https://resy.com/',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'X-Resy-Universal-Auth': accToken,
        'Content-Type': 'application/x-www-form-urlencoded',
    }

    data = {
        'stripe_payment_method_id': pm,
    }
    response = requests.post('https://api.resy.com/3/stripe/payment_method', headers=headers, data=data, proxies=proxiesObj, verify=False)
    print(f'Final Response: {response.text}')

""" def get_captcha_token(captcha_key, site_key, url, proxies):
    proxy = random.choice(proxies)
    payload = {
        "clientKey": captcha_key,
        "task": {
            "type": "RecaptchaV2Task",
            "websiteKey": site_key,
            "websiteURL": url,
            "proxy": f'{proxy}'
        }
    }

    res = requests.post('https://api.capsolver.com/createtask', json=payload)
    print(f'res: {res}')
    print(f'res text: {res.text}')
    resp = res.json()
    print(f'resp: {resp}')
    print
    task_id = resp.get('taskId')
    if not task_id:
        print(f'Failed to get CAPTCHA token: {resp}')
        return
    print(f"Got taskId: {task_id} / Getting result...")

    while True:
        time.sleep(3)  # delay
        payload = {"clientKey": captcha_key, "taskId": task_id}
        res = requests.post("https://api.capsolver.com/getTaskResult", json=payload)
        resp = res.json()
        status = resp.get("status")
        if status == "ready":
            print(f'Solved, response: {resp.get("solution")}')
            return resp.get("solution", {}).get('gRecaptchaResponse')
        if status == "failed" or resp.get("errorId"):
            print("Solve failed! response:", res.text)
            return """

def list_reservations():
    accounts = load_data(ACCOUNTS_FILE, [])
    if not accounts:
        click.echo('No accounts found. Please add accounts before listing reservations.')
        return
    all_reservations = []
    # Get all reservations for each account
    for account in accounts:
        auth_token = account['auth_token']
        account_name = account['account_name']
        reservations = get_account_reservations(auth_token, account_name)
        all_reservations.extend(reservations)
    
    save_data(RESERVATIONS_FILE, all_reservations)
    
    if all_reservations:
        show_reservations()

def show_reservations():
    reservations = load_data(RESERVATIONS_FILE, [])
    click.clear()
    if not reservations:
        click.echo('No reservations found.')
        return

    while True:
        click.echo(click.style('Reservations', bold=True, fg='cyan'))

        # Create a list of reservation choices
        res_choices = []
        for idx, res in enumerate(reservations):
            res_info = f'{idx + 1}) Email: {res["email"]}, Venue: {res["venue"]}, Day: {res["day"]}, Time Slot: {res["time_slot"]}, Seats: {res["num_seats"]}, Link: {res["link"]}'
            if 'cancel_by' in res:
                res_info += f', Cancel By: {res["cancel_by"]}'
            res_choices.append(res_info)

        # Add a 'Back' option
        res_choices.append('Back')

        # Prompt user to select a reservation or go back
        questions = [
            inquirer.List('res_choice',
                          message='Select a reservation or choose Back',
                          choices=res_choices)
        ]
        answers = inquirer.prompt(questions)

        if answers['res_choice'] == 'Back':
            return

        # Extract reservation index and details
        res_index = int(answers['res_choice'].split(')')[0]) - 1
        res = reservations[res_index]

        # Show reservation details and options
        action = show_reservation_details(res)
        
        if action == 'cancel':
            # Remove the cancelled reservation from the list (thread-safe)
            resy_token_to_remove = res['resy_token']
            def remove_by_token(reservations_data):
                return [r for r in reservations_data if r['resy_token'] != resy_token_to_remove]
            reservations = update_data(RESERVATIONS_FILE, [], remove_by_token)
            # If all reservations are cancelled, exit the function
            if not reservations:
                click.echo('No more reservations.')
                return
        # If action is not 'cancel', it's 'back', so we continue the loop

        

def show_reservation_details(res):
    while True:
        click.clear()
        click.echo(click.style('Reservation Details', bold=True, fg='cyan'))
        click.echo(f'Email: {res["email"]}')
        click.echo(f'Venue: {res["venue"]}')
        click.echo(f'Day: {res["day"]}')
        click.echo(f'Time Slot: {res["time_slot"]}')
        click.echo(f'Seats: {res["num_seats"]}')
        click.echo(f'Link: {res["link"]}')
        if 'cancel_by' in res:
            click.echo(f'Cancel By: {res["cancel_by"]}')

        action_questions = [
            inquirer.List('res_action',
                          message='Choose an option',
                          choices=['Cancel reservation', 'Back to list'])
        ]
        action_answers = inquirer.prompt(action_questions)
        if action_answers['res_action'] == 'Cancel reservation':
            cancel_reservation(res['auth_token'], res['resy_token'])
            return 'cancel'  # Indicate that the reservation was cancelled
        elif action_answers['res_action'] == 'Back to list':
            return 'back'  # Go back to the reservation list


def cancel_reservation(auth_token, resy_token):
    click.echo('Cancelling reservation...')
    proxy = get_random_proxy()
    headers = {
        'Host': 'api.resy.com',
        'Accept': 'application/json, text/plain, */*',
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'Sec-Fetch-Site': 'same-site',
        'X-Origin': 'https://resy.com',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'Sec-Fetch-Mode': 'cors',
        'X-Resy-Auth-Token': auth_token,
        'Origin': 'https://resy.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Referer': 'https://resy.com/',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'X-Resy-Universal-Auth': auth_token,
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    data = {
        'resy_token': resy_token,
    }
    try:
        response = requests.post('https://api.resy.com/3/cancel', headers=headers, data=data, proxies=proxy, verify=False)
        if response.status_code == 200:
            click.echo(click.style('Reservation cancelled successfully!', fg='green'))
            # Thread-safe removal from reservations file
            def remove_reservation(reservations):
                return [res for res in reservations if res['resy_token'] != resy_token]
            update_data(RESERVATIONS_FILE, [], remove_reservation)
        else:
            click.echo(click.style('Failed to cancel reservation.', fg='red'))
    except Exception as e:
        click.echo(click.style(f'Error cancelling reservation: {e}', fg='red'))
    input('Press Enter to continue...')
        
def get_account_reservations(auth_token, account_name):
    proxy = get_random_proxy()
    headers = {
        'Host': 'api.resy.com',
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'Sec-Fetch-Site': 'same-site',
        'X-Origin': 'https://resy.com',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'X-Resy-Auth-Token': auth_token,
        'Sec-Fetch-Mode': 'cors',
        'Origin': 'https://resy.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        'Referer': 'https://resy.com/',
        'Connection': 'keep-alive',
        'Accept': 'application/json, text/plain, */*',
        'Sec-Fetch-Dest': 'empty',
        'X-Resy-Universal-Auth': auth_token,
    }

    params = {
        'type': 'upcoming',
    }

    response = requests.get('https://api.resy.com/3/user/reservations', params=params, headers=headers, proxies=proxy, verify=False)
    res = response.json()

    if 'reservations' not in res:
        print(f'Failed to get reservations for {account_name}: {res}')
        return []

    account_reservations = []
    for reservation in res['reservations']:
        venue_id = str(reservation['venue']['id'])
        res = {
            'resy_token': reservation['resy_token'],
            'auth_token': auth_token,
            #'venue': reservation['venue']['id'],
            'venue': res['venues'][venue_id]['name'],
            'first_name': reservation['party'][0]['first_name'],
            'last_name': reservation['party'][0]['last_name'],
            'email': reservation['party'][0]['user']['em_address'],
            'day': reservation['day'],
            'time_slot': reservation['time_slot'],
            'num_seats': reservation['num_seats'],
            'link': reservation['share']['link'],
        }

        if 'cancellation' in reservation and reservation['cancellation'] and 'date_refund_cut_off' in reservation['cancellation']:
            res['cancel_by'] = reservation['cancellation']['date_refund_cut_off']
        account_reservations.append(res)
        
    return account_reservations

def schedule_tasks():
    tasks = load_data(TASKS_FILE, [])
    if not tasks:
        click.echo('No tasks found. Please add tasks before scheduling.')
        return

    questions = [
        inquirer.List('task_index',
                      message="Select a task to schedule:",
                      choices=[(f"Task {i+1}: {task.get('task_name', task['restaurant_id'])}", i) for i, task in enumerate(tasks)]),
        inquirer.List('repeat',
                      message="Repeat schedule?",
                      choices=['Daily', 'Weekly', 'Once']),
        inquirer.Text('duration', message="Enter task duration in seconds (5-10 recommended):", default="10")
    ]
    answers = inquirer.prompt(questions)

    # Ask for date if "Once" is selected
    if answers['repeat'] == 'Once':
        date_question = [
            inquirer.Text('schedule_date', message="Enter the date to run (YYYY-MM-DD, or press Enter for today):", default="")
        ]
        date_answer = inquirer.prompt(date_question)
        schedule_date_str = date_answer['schedule_date']
    else:
        schedule_date_str = ""

    time_question = [
        inquirer.Text('schedule_time', message="Enter the time to schedule (HH:MM):")
    ]
    time_answer = inquirer.prompt(time_question)

    task_index = answers['task_index']
    schedule_time = datetime.datetime.strptime(time_answer['schedule_time'], "%H:%M").time()
    duration = int(answers['duration'])

    job_id = f"task_{task_index}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

    if answers['repeat'] == 'Daily':
        scheduler.add_job(start_and_stop_task, 'cron', args=[task_index, duration, job_id],
                          hour=schedule_time.hour, minute=schedule_time.minute, id=job_id)
    elif answers['repeat'] == 'Weekly':
        scheduler.add_job(start_and_stop_task, 'cron', args=[task_index, duration, job_id],
                          day_of_week='mon-sun', hour=schedule_time.hour, minute=schedule_time.minute, id=job_id)
    else:  # Once
        if schedule_date_str:
            schedule_date = datetime.datetime.strptime(schedule_date_str, "%Y-%m-%d").date()
        else:
            schedule_date = datetime.date.today()
        next_run = datetime.datetime.combine(schedule_date, schedule_time)
        if next_run <= datetime.datetime.now():
            next_run += datetime.timedelta(days=1)
        scheduler.add_job(start_and_stop_task, 'date', args=[task_index, duration, job_id],
                          run_date=next_run, id=job_id)

    # Save to file for persistence
    save_scheduled_task(job_id, task_index, time_answer['schedule_time'], answers['repeat'], duration, schedule_date_str)

    if answers['repeat'] == 'Once' and schedule_date_str:
        click.echo(f"Task scheduled to run on {schedule_date_str} at {schedule_time.strftime('%H:%M')} for {duration} seconds")
    else:
        click.echo(f"Task scheduled to run at {schedule_time.strftime('%H:%M')} {answers['repeat']} for {duration} seconds")

def start_and_stop_task(task_index, duration, job_id):
    task_thread = threading.Thread(target=run_task_with_timeout, args=(task_index, duration, job_id))
    task_thread.start()

def run_task_with_timeout(task_index, duration, job_id):
    tasks = load_data(TASKS_FILE, [])
    proxies = load_data(PROXIES_FILE, [])
    info = load_data(INFO_FILE, {})

    if task_index >= len(tasks):
        print(f"Error: Task index {task_index} out of range")
        return

    # Make proxies None if empty so task_executor handles it correctly
    if not proxies:
        proxies = None

    task = tasks[task_index]

    # Thread-safe addition to running_tasks
    with _running_tasks_lock:
        running_tasks[job_id] = {
            'thread': threading.current_thread(),
            'start_time': time.time(),
            'duration': duration,
            'task': task
        }

    try:
        run_tasks_concurrently([task], info['capsolver_key'], info['capmonster_key'], proxies, info['discord_webhook'])
    except Exception as e:
        print(f"Error starting scheduled task: {e}")
    finally:
        time.sleep(duration)
        print(f"Task {job_id} completed after {duration} seconds")
        # Thread-safe removal from running_tasks
        with _running_tasks_lock:
            running_tasks.pop(job_id, None)

# Reload scheduled tasks on startup (must be after start_and_stop_task is defined)
reload_scheduled_tasks()

def view_scheduled_tasks():
    while True:
        click.clear()
        click.echo(click.style('Scheduled Tasks', bold=True, fg='cyan'))
        jobs = scheduler.get_jobs()
        tasks = load_data(TASKS_FILE, [])
        scheduled_tasks_data = load_data(SCHEDULED_TASKS_FILE, [])

        if not jobs:
            click.echo("No scheduled tasks.")
        else:
            for i, job in enumerate(jobs):
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "N/A"
                # Find task name from scheduled task data
                task_name = "Unknown"
                for st in scheduled_tasks_data:
                    if st['job_id'] == job.id:
                        task_index = st['task_index']
                        if task_index < len(tasks):
                            task_name = tasks[task_index].get('task_name', f"Restaurant {tasks[task_index]['restaurant_id']}")
                        break
                click.echo(f"{i+1}) {task_name} - Next run: {next_run}")
        
        click.echo("\nRunning Tasks:")
        # Thread-safe copy of running_tasks for iteration
        with _running_tasks_lock:
            running_tasks_snapshot = dict(running_tasks)
        for i, (job_id, task_info) in enumerate(running_tasks_snapshot.items()):
            elapsed = time.time() - task_info['start_time']
            click.echo(f"{i+1}) Task ID: {job_id}, Running for: {elapsed:.2f}s, Max duration: {task_info['duration']}s")
        
        questions = [
            inquirer.List('action',
                          message="Choose an action",
                          choices=['Remove scheduled task', 'Stop running task', 'Back'],
                          carousel=True)
        ]
        answers = inquirer.prompt(questions)
        
        if answers['action'] == 'Remove scheduled task':
            remove_scheduled_task(jobs)
        elif answers['action'] == 'Stop running task':
            stop_running_task()
        elif answers['action'] == 'Back':
            break

def remove_scheduled_task(jobs):
    if not jobs:
        click.echo("No scheduled tasks to remove.")
        time.sleep(2)
        return

    tasks = load_data(TASKS_FILE, [])
    scheduled_tasks_data = load_data(SCHEDULED_TASKS_FILE, [])

    choices = []
    for i, job in enumerate(jobs):
        task_name = "Unknown"
        for st in scheduled_tasks_data:
            if st['job_id'] == job.id:
                task_index = st['task_index']
                if task_index < len(tasks):
                    task_name = tasks[task_index].get('task_name', f"Restaurant {tasks[task_index]['restaurant_id']}")
                break
        choices.append((f"{task_name} ({job.id})", job.id))

    questions = [
        inquirer.List('job_id',
                      message="Select a task to remove",
                      choices=choices)
    ]
    answers = inquirer.prompt(questions)
    
    scheduler.remove_job(answers['job_id'])
    remove_scheduled_task_from_file(answers['job_id'])
    click.echo(f"Removed scheduled task: {answers['job_id']}")
    time.sleep(2)

def stop_running_task():
    # Thread-safe check and snapshot
    with _running_tasks_lock:
        if not running_tasks:
            click.echo("No running tasks to stop.")
            time.sleep(2)
            return
        choices = [(f"Task {i+1}: {job_id}", job_id) for i, (job_id, _) in enumerate(running_tasks.items())]

    questions = [
        inquirer.List('job_id',
                      message="Select a task to stop",
                      choices=choices)
    ]
    answers = inquirer.prompt(questions)

    job_id = answers['job_id']
    # Thread-safe access and removal
    with _running_tasks_lock:
        task_info = running_tasks.get(job_id)
        if task_info:
            thread = task_info['thread']
        else:
            click.echo(f"Task {job_id} is no longer running.")
            time.sleep(2)
            return

    thread.join(0.1)  # Give the thread a chance to finish (outside lock to avoid deadlock)

    with _running_tasks_lock:
        running_tasks.pop(job_id, None)
    click.echo(f"Stopped running task: {job_id}")
    time.sleep(2)

def start_tasks():
    tasks = load_data(TASKS_FILE, [])
    proxies = load_data(PROXIES_FILE, [])
    info = load_data(INFO_FILE, {})
    
    if not tasks:
        click.echo('No tasks found. Please add tasks before starting.')
        return
    if not proxies:
        click.echo('No proxies found. Running without proxies.')
        proxies = None
    if not info:
        click.echo('No user info found. Please set user info before starting.')
        return

    try:
        run_tasks_concurrently(tasks, info['capsolver_key'], info['capmonster_key'], proxies, info['discord_webhook'])
    except Exception as e:
        print(f"Error starting tasks: {e}")
    
    input('Press Enter to continue...')
    



if __name__ == '__main__':
    cli()
