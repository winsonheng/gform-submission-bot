import datetime
import json
import logging
import os
import re
import requests
from pathlib import Path
from pytz import timezone
from telegram import Update
from telegram.ext import Updater, CallbackContext, CommandHandler, MessageHandler, Filters, ConversationHandler
from gformhelper import GFormHelper

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(funcName)s() - %(lineno)d - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# region initialise constants

ERROR_MESSAGE_TO_USERS = "An error has occurred. Please try again. The administrator will be notified of the issue."
ERROR_OPTIONS_NOT_FOUND = "Options list not found in user data."

REMINDER_JOB_NAME_REGEX = re.compile("[0-9]{2}:[0-9]{2}")
REMINDER_USER_LIMIT = 50  # Arbitrary limit to number of users who can receive reminders at any given minute

STATE_SET_GROUP = "set_group"
STATE_SET_NAME = "set_name"
STATE_SET_REMINDER_AM = "set_reminder_am"
STATE_SET_REMINDER_PM = "set_reminder_pm"
STATE_SET_TEMPERATURE_AM = "set_temperature_am"
STATE_SET_TEMPERATURE_PM = "set_temperature_pm"

USER_DATA_CONFIG_DONE = "config_done"
USER_DATA_CURRENT_STATE = "current_state"
USER_DATA_OPTIONS = "options"
USER_DATA_GROUP = "group"
USER_DATA_NAME = "name"
USER_DATA_REMINDER_AM = "reminder_am"
USER_DATA_REMINDER_PM = "reminder_pm"
USER_DATA_SUBMITTED_TEMPERATURE_AM = "submitted_am"
USER_DATA_SUBMITTED_TEMPERATURE_PM = "submitted_pm"

STATUS_SUBMIT_OK = 200  # Form successfully submitted status

TEMPERATURES = [str(x / 10) for x in range(360, 400)]

FORM_URL = os.getenv("FORM_URL", "")
APP_NAME = os.getenv("APP_NAME", "")
TOKEN = os.getenv("TOKEN", "")
URL = "https://api.telegram.org/bot{}/".format(TOKEN)
PORT = int(os.getenv('PORT', '8443'))
MODE = os.getenv("MODE", "dev")
TIMEZONE = timezone("Singapore")

# endregion initialise constants

questions = {}  # dict of question_name : question_id
pages = {}  # dict of question_name : page_num

updater = Updater(token=TOKEN, use_context=True)
dispatcher = updater.dispatcher
scheduler = updater.job_queue
scheduler.start()

gform = GFormHelper(FORM_URL)


def daily_night_reset(context: CallbackContext):
    """
    Called at 23:55 every night
    1. Updates form data and check for all users if their name is still in their specified group namelist
    2. Resets all submit checks to false
    3. Removes all unnecessary jobs

    :param context: CallbackContext containing current job
    """

    # Get updated namelist of each group
    gform.update_list()
    names_by_group = {}
    groups_and_next_question_id = gform.get_options_and_next_question_id(questions["GROUP"])
    for group in groups_and_next_question_id:
        names_by_group[group] = gform.get_options(groups_and_next_question_id[group])

    # 1. Scan through jobqueue for unnecessary jobs (those containing "extra_reminder") and delete them
    # 2. Check if each person's name is in the same group as configured, if not, inform them to reconfigure

    jobs_to_remove = []
    for job in scheduler.jobs():
        if "extra_reminder" in job.name:
            jobs_to_remove.append(job)
        else:
            if job.context and isinstance(job.context, tuple) and len(job.context) == 2:
                user_update, user_context = context.job.context
            else:
                continue

            user_context[USER_DATA_SUBMITTED_TEMPERATURE_AM] = False
            user_context[USER_DATA_SUBMITTED_TEMPERATURE_PM] = False

            if USER_DATA_CONFIG_DONE:
                if user_context[USER_DATA_GROUP] not in names_by_group:
                    user_update.message.reply_text("⚠️Warning: your selected group is no longer an option in the form. "
                                                   "Please inform your form administrator or reconfigure using /config")
                elif user_context[USER_DATA_NAME] not in names_by_group[user_context[USER_DATA_GROUP]]:
                    user_update.message.reply_text("⚠️Warning: your name is not found in your selected group namelist. "
                                                   "Please inform your form administrator or reconfigure using /config")

    for job in jobs_to_remove:
        job.schedule_removal()

    logger.info("Night reset complete!")


def send_reminder(context: CallbackContext):
    """
    Sends a reminder to a user and creates an additional reminder 1hr later if needed
    Extra reminders are sent on an hourly basis until the next submission window or the user responds
    Format of context.job.name as follows:
    -Normal reminder: time
    -Extra reminders: time-extra_reminder-next_time

    :param context: CallbackContext containing current job
    """

    if not REMINDER_JOB_NAME_REGEX.match(context.job.name):
        return

    job_context = context.job.context
    if job_context and isinstance(job_context, tuple) and len(job_context) == 2:
        user_update, user_context = context.job.context
    else:
        return

    reminder_time = context.job.name
    next_reminder_time = str(int(reminder_time[:2]) + 1).zfill(2) + reminder_time[2:]
    am_or_pm = "am" if int(reminder_time[:2]) < 12 else "pm"

    # If is an extra reminder, remove from the jobqueue
    if "-extra_reminder-" in reminder_time:
        reminder_time, next_reminder_time = reminder_time.split("-extra_reminder-")
        next_reminder_time = str(int(next_reminder_time[:2]) + 1).zfill(2) + next_reminder_time[2:]
        context.job.schedule_removal()

    """
    If anyone using this reminder has not submitted yet,
    1. Schedule an extra reminder 1hr later ONLY IF it is still within the current submission window
    2. Send them a reminder
    """

    if not user_context.user_data[
        USER_DATA_SUBMITTED_TEMPERATURE_AM if am_or_pm == "am" else USER_DATA_SUBMITTED_TEMPERATURE_PM]:
        if am_or_pm == "am" and int(next_reminder_time[:2]) < 12 or \
                am_or_pm == "pm" and int(next_reminder_time[:2]) < 24:
            scheduler.run_once(send_reminder, when=3600,
                               name=reminder_time + "-extra_reminder-" + next_reminder_time)
            force_submit(user_update, user_context)


# region functions used for temperature submission

def set_temperature(update: Update, context: CallbackContext):
    """
    Function called when a valid temperature is sent and user state is SET_REMINDER_<AM/PM>
    Calls set_temperature_am or set_temperature_pm accordingly

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: Conversation.END if submission successful or user ineligible for submission
             STATE_SET_TEMPERATURE_<AM/PM> if submission unsuccessful
    """

    if context.user_data.get(USER_DATA_CURRENT_STATE):
        if context.user_data[USER_DATA_CURRENT_STATE] == STATE_SET_TEMPERATURE_AM:
            return set_temperature_am(update, context)
        elif context.user_data[USER_DATA_CURRENT_STATE] == STATE_SET_TEMPERATURE_PM:
            return set_temperature_pm(update, context)
    return ConversationHandler.END


def set_temperature_am(update: Update, context: CallbackContext):
    """
    Function called to submit AM temperature

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: Conversation.END if submission successful
             STATE_SET_TEMPERATURE_AM if submission unsuccessful
    """

    if update.message.text in TEMPERATURES:
        status_code = submit_form(update, context, update.message.text, "am")

        if status_code == 200:
            # Submission ok
            # Update the submitted_am field to True
            context.user_data[USER_DATA_SUBMITTED_TEMPERATURE_AM] = True
            current_time = datetime.datetime.now(TIMEZONE)
            update.message.reply_text("🗓️ {} — AM\n\n🌡️ {}°C\n\n✅ Submitted successfully\n\nTo re-submit, enter "
                                      "/force_submit\nTo re-configure your details, enter /config".format(
                                        current_time.strftime("%d/%m/%y"), update.message.text))
        else:
            update.message.reply_text("❌ Error occurred while submitting. Please try again later.\n\nNote: if you have "
                                      "recently changed your details, please reconfigure using /config")
        return ConversationHandler.END
    else:
        update.message.reply_text("Invalid temperature!")
        return force_submit(update, context)


def set_temperature_pm(update: Update, context: CallbackContext):
    """
    Function called to submit PM temperature

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: Conversation.END if submission successful
             STATE_SET_TEMPERATURE_PM if submission unsuccessful
    """

    if update.message.text in TEMPERATURES:
        status_code = submit_form(update, context, update.message.text, "pm")

        if status_code == 200:
            # Submission ok
            # Update the submitted_am field to True
            context.user_data[USER_DATA_SUBMITTED_TEMPERATURE_PM] = True
            current_time = datetime.datetime.now(TIMEZONE)
            update.message.reply_text("🗓️ {} — PM\n\n🌡️ {}°C\n\n✅ Submitted successfully\n\nTo re-submit, enter "
                                      "/force_submit\nTo re-configure your details, enter /config".format(
                current_time.strftime("%d/%m/%y"), update.message.text))
        else:
            update.message.reply_text("❌ Error occurred while submitting. Please try again later.\n\nNote: if you have "
                                      "recently changed your details, please reconfigure using /config")
        return ConversationHandler.END
    else:
        update.message.reply_text("Invalid temperature!")
        return force_submit(update, context)


def submit_form(update: Update, context: CallbackContext, temperature, am_or_pm="AM"):
    """
    Function called to submit the Google Form with user's data and temperature

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :param temperature: user's submitted temperature
    :param am_or_pm: timeframe for the current submission
    :return: status code from the POST request
    """

    am_or_pm = am_or_pm.upper()
    current_time = datetime.datetime.now(TIMEZONE)

    submit_data = {"entry.{}".format(questions["GROUP"]): context.user_data[USER_DATA_GROUP],
                   "entry.{}".format(questions[context.user_data[USER_DATA_GROUP]]): context.user_data[USER_DATA_NAME],
                   "entry.{}.year".format(questions["DATE"]): current_time.year,
                   "entry.{}.month".format(questions["DATE"]): current_time.month,
                   "entry.{}.day".format(questions["DATE"]): current_time.day,
                   "entry.{}".format(questions["AM OR PM"]): am_or_pm}

    if float(temperature) < 36.0 or float(temperature) > 37.5:
        submit_data["entry.{}.other_option_response".format(questions["{} TEMP".format(am_or_pm)])] = temperature
        submit_data["entry.{}".format(questions["{} TEMP".format(am_or_pm)])] = "__other_option__"
    else:
        submit_data["entry.{}".format(questions["{} TEMP".format(am_or_pm)])] = temperature

    submit_data["pageHistory"] = ",".join([pages["GROUP"], pages[context.user_data[USER_DATA_GROUP]],
                                           pages["DATE"], pages["{} TEMP".format(am_or_pm)]])
    return gform.submit_form(submit_data)


def force_submit(update: Update, context: CallbackContext):
    """
    Called during the command /force_submit or when the user is required to retry an unsuccessful submission

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: STATE_SET_TEMPERATURE_<AM/PM> depending on time of day
             Conversation.END if user ineligible to submit
    """

    if context.user_data.get(USER_DATA_CONFIG_DONE):
        current_time = datetime.datetime.now(TIMEZONE)
        am_or_pm = "am" if current_time.hour < 12 else "pm"
        update.message.reply_text(
            "It is {}, you are eligible for {} submission.\n\nPlease enter your temperature:".format(
                current_time.strftime("%d/%m/%Y, %H:%M"), am_or_pm.upper()), reply_markup=temperature_keyboard())
        context.user_data[
            USER_DATA_CURRENT_STATE] = STATE_SET_TEMPERATURE_AM if am_or_pm == "am" else STATE_SET_TEMPERATURE_PM
        return context.user_data[USER_DATA_CURRENT_STATE]
    else:
        update.message.reply_text("Seems like you have not configured or something went wrong.\n\n"
                                  "Please enter /config or try again later")
    return ConversationHandler.END


# endregion functions used for temperature submission

def start(update: Update, context: CallbackContext):
    """
    Called on /start

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    """

    update.message.reply_text("Welcome to the Temperature Bot.\nTo get started, enter /config")


# region functions used when user enters /stop

def stop(update: Update, context: CallbackContext):
    """
    Called on /stop

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    """

    update.message.reply_text("Thank you for using Temperature Bot. You will no longer receive reminders.")
    clear_user_data(update, context)
    return ConversationHandler.END


def clear_user_data(update: Update, context: CallbackContext):
    """
    Called during execution of stop() to remove user's data and job used for reminders

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    """
    context.user_data.clear()
    remove_reminders_from_jobqueue(update.message.chat.id)


def remove_reminders_from_jobqueue(chat_id):
    """
    Removes all reminders from jobqueue belonging to a specified chat_id

    :param chat_id: target chat id
    """
    for job in scheduler.jobs():
        if job.context and isinstance(job.context, tuple) and len(job.context) == 2:
            job_update, job_context = job.context
            if chat_id == job_update.message.chat.id:
                job.schedule_removal()


# endregion functions used when user enters /stop

# region user configuration

def config(update: Update, context: CallbackContext):
    """
    Called on /config
    Clears all previously configured user data and removes reminders belonging to the user
    Asks user which group he/she belongs to

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: STATE_SET_GROUP
    """

    if context.user_data.get(USER_DATA_CONFIG_DONE):
        # User has configured before, remove the reminders from jobqueue
        remove_reminders_from_jobqueue(update.message.chat.id)

    context.user_data[USER_DATA_CURRENT_STATE] = ""
    context.user_data[USER_DATA_CONFIG_DONE] = False
    context.user_data[USER_DATA_SUBMITTED_TEMPERATURE_AM] = False
    context.user_data[USER_DATA_SUBMITTED_TEMPERATURE_PM] = False

    # Delete both AM and PM reminders
    if context.user_data.get(USER_DATA_REMINDER_AM):
        del context.user_data[USER_DATA_REMINDER_AM]
    if context.user_data.get(USER_DATA_REMINDER_PM):
        del context.user_data[USER_DATA_REMINDER_PM]

    for job in scheduler.jobs():
        # Loop through jobQueue and remove jobs for the current user
        if isinstance(job.context, list) and len(job.context) == 2:
            job_user_id = job.context[0].message.chat.id
            if job_user_id == update.message.chat.id:
                job.schedule_removal()

    options_dict = gform.get_options_and_next_question_id(questions["GROUP"])
    for option in options_dict:
        # Allows dynamic updates in question list should options be added to the question
        if option[0] not in questions:
            questions[option[0]] = option[1]

    group_options = list(options_dict.keys())
    context.user_data[USER_DATA_OPTIONS] = group_options
    update.message.reply_text("Select your group:", reply_markup=build_keyboard(group_options))
    return STATE_SET_GROUP


def set_group(update: Update, context: CallbackContext):
    """
    Called when user state at STATE_SET_GROUP
    Sets group in user data if valid and asks for name, else ask for group again

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: STATE_SET_NAME if valid response
             STATE_SET_GROUP otherwise
    """

    if not context.user_data.get(USER_DATA_OPTIONS):
        update.message.reply_text(ERROR_MESSAGE_TO_USERS)
        logger.error(ERROR_OPTIONS_NOT_FOUND)
        return

    options_dict = gform.get_options_and_next_question_id(questions["GROUP"])

    if update.message.text in context.user_data[USER_DATA_OPTIONS]:
        context.user_data[USER_DATA_GROUP] = update.message.text
        name_options = gform.get_options(options_dict[update.message.text])
        context.user_data[USER_DATA_OPTIONS] = name_options
        update.message.reply_text("Select your name", reply_markup=build_keyboard(name_options))
        return STATE_SET_NAME
    else:
        update.message.reply_text("Select your group:",
                                  reply_markup=build_keyboard(context.user_data[USER_DATA_OPTIONS]))
        return STATE_SET_GROUP


def set_name(update: Update, context: CallbackContext):
    """
    Called when user state at STATE_SET_NAME
    Sets name in user data if valid and asks for AM reminder time, else ask for name again

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: STATE_SET_REMINDER_AM if valid response
             STATE_SET_NAME otherwise
    """

    if not context.user_data.get(USER_DATA_OPTIONS):
        update.message.reply_text(ERROR_MESSAGE_TO_USERS)
        logger.error(ERROR_OPTIONS_NOT_FOUND)
        return

    if update.message.text in context.user_data[USER_DATA_OPTIONS]:
        context.user_data[USER_DATA_NAME] = update.message.text
        am_reminder_options = [str(i).zfill(2) + ":00" for i in range(12)]
        context.user_data[USER_DATA_OPTIONS] = am_reminder_options
        update.message.reply_text("Set your daily AM temperature reminder:",
                                  reply_markup=build_keyboard(am_reminder_options))
        return STATE_SET_REMINDER_AM
    else:
        update.message.reply_text("Select your name", reply_markup=build_keyboard(context.user_data[USER_DATA_OPTIONS]))
        return STATE_SET_NAME


def set_reminder_am(update: Update, context: CallbackContext):
    """
    Called when user state at STATE_SET_REMINDER_AM
    Sets AM reminder in user data if valid and asks for PM reminder time, else ask for AM reminder time again

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: STATE_SET_REMINDER_PM if valid response
             STATE_SET_REMINDER_AM otherwise
    """

    if not context.user_data[USER_DATA_OPTIONS]:
        update.message.reply_text(ERROR_MESSAGE_TO_USERS)
        logger.error(ERROR_OPTIONS_NOT_FOUND)
        return

    if update.message.text in context.user_data[USER_DATA_OPTIONS]:
        context.user_data[USER_DATA_REMINDER_AM] = update.message.text
        pm_reminder_options = [str(i).zfill(2) + ":00" for i in range(12, 24)]
        context.user_data[USER_DATA_OPTIONS] = pm_reminder_options
        update.message.reply_text("Set your daily PM temperature reminder:",
                                  reply_markup=build_keyboard(pm_reminder_options))
        return STATE_SET_REMINDER_PM
    else:
        update.message.reply_text("Set your daily AM temperature reminder:",
                                  reply_markup=build_keyboard(context.user_data[USER_DATA_OPTIONS]))
        return STATE_SET_REMINDER_AM


def set_reminder_pm(update: Update, context: CallbackContext):
    """
    Called when user state at STATE_SET_REMINDER_PM
    Sets PM reminder in user data if valid, else ask for PM reminder time again
    Creates reminders for both AM and PM periods
    If the current submission period is overdue, calls force_submit() to ask user to submit

    :param update: Update containing message data
    :param context: CallbackContext containing user data
    :return: Conversation.END if valid response and user does not need to send backlog reminder
             STATE_SET_TEMPERATURE_<AM/PM> if valid response and user needs to send backlog reminder
             STATE_SET_REMINDER_PM otherwise
    """

    if not context.user_data[USER_DATA_OPTIONS]:
        update.message.reply_text(ERROR_MESSAGE_TO_USERS)
        logger.error(ERROR_OPTIONS_NOT_FOUND)
        return

    if update.message.text in context.user_data[USER_DATA_OPTIONS]:
        context.user_data[USER_DATA_REMINDER_PM] = update.message.text
    else:
        update.message.reply_text("Set your daily PM temperature reminder:",
                                  reply_markup=build_keyboard(context.user_data[USER_DATA_OPTIONS]))
        return STATE_SET_REMINDER_PM

    # Creating the am reminder

    time_requested = context.user_data[USER_DATA_REMINDER_AM]

    is_set = False
    next_inactive_minute = 0  # The next possible minute that is not being used
    list_of_active_timeslots = list(set(filter(lambda x: REMINDER_JOB_NAME_REGEX.match(x)
                                                         and x[:2] == time_requested[:2],
                                               map(lambda x: x.name, scheduler.jobs()))))
    for timeslot in list_of_active_timeslots:
        # Check if any of the active timeslots can accept one more user
        if len(scheduler.get_jobs_by_name(timeslot)) < REMINDER_USER_LIMIT:
            # Set reminder if the number of people using this timeslot is below the limit
            context.user_data[USER_DATA_REMINDER_AM] = timeslot
            is_set = True
            break
        else:
            next_inactive_minute = (int(timeslot[3:]) + 1) % 60

    if not is_set:
        # Reminder has not been set in an active timeslot -> set reminder in next possible timeslot
        context.user_data[USER_DATA_REMINDER_AM] = time_requested[:2] + ":" + str(next_inactive_minute).zfill(2)

    reminder_am = context.user_data[USER_DATA_REMINDER_AM]

    context.job_queue.run_daily(send_reminder,
                                time=datetime.time(hour=int(reminder_am[:2]), minute=int(reminder_am[3:]),
                                                   second=0, tzinfo=TIMEZONE), context=(update, context),
                                name=reminder_am)

    current_time = datetime.datetime.now(TIMEZONE)

    # Creating the pm reminder

    time_requested = update.message.text

    is_set = False
    next_inactive_minute = 0  # The next possible minute that is not being used
    list_of_active_timeslots = list(set(filter(lambda x: REMINDER_JOB_NAME_REGEX.match(x)
                                                         and x[:2] == time_requested[:2],
                                               map(lambda x: x.name, scheduler.jobs()))))
    for timeslot in list_of_active_timeslots:
        # Check if any of the active timeslots can accept one more user
        if len(scheduler.get_jobs_by_name(timeslot)) < REMINDER_USER_LIMIT:
            # Set reminder if the number of people using this timeslot is below the limit
            context.user_data[USER_DATA_REMINDER_PM] = timeslot
            is_set = True
            break
        else:
            next_inactive_minute = (int(timeslot[3:]) + 1) % 60

    if not is_set:
        # Reminder has not been set in an active timeslot -> set reminder in next possible timeslot
        context.user_data[USER_DATA_REMINDER_PM] = time_requested[:2] + ":" + str(next_inactive_minute).zfill(2)

    reminder_pm = context.user_data[USER_DATA_REMINDER_PM]

    context.job_queue.run_daily(send_reminder,
                                time=datetime.time(hour=int(reminder_pm[:2]), minute=int(reminder_pm[3:]),
                                                   second=0, tzinfo=TIMEZONE), context=(update, context),
                                name=reminder_pm)

    context.user_data[USER_DATA_CONFIG_DONE] = True

    update.message.reply_text("Successfully finished setup!")
    update.message.reply_text("You will be sent a reminder at {} and {} everyday.\n\nYou may also use /force_submit to "
                              "manually submit your temperature.".format(reminder_am, reminder_pm))

    # Send a backlog reminder if reminder time < current time

    current_time = datetime.datetime.now(TIMEZONE)
    am_or_pm = "am" if current_time.hour < 12 else "pm"

    if (am_or_pm == "am" and (int(reminder_am[:2]) < current_time.hour or int(reminder_am[:2]) == current_time.hour
                              and int(reminder_am[3:]) <= current_time.minute)) or (
            am_or_pm == "pm" and (int(reminder_pm[:2]) < current_time.hour or int(reminder_pm[:2]) ==
                                  current_time.hour and int(reminder_pm[3:]) <= current_time.minute)):
        return force_submit(update, context)

    return ConversationHandler.END


# endregion configuration


def do_not_sleep(context: CallbackContext):
    """
    Sends a GET request to Heroku to prevent program from sleeping

    :param context: CallbackContext containing user data
    """
    requests.get("https://{}.herokuapp.com/{}".format(APP_NAME, TOKEN))


def error(update, context):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)


# region keyboard helper functions

def build_keyboard(items):
    keyboard = [[item] for item in items]
    reply_markup = {"keyboard": keyboard, "one_time_keyboard": True}
    return json.dumps(reply_markup)


def temperature_keyboard():
    keyboard = [[str(x / 10), str((x + 1) / 10)] for x in range(360, 400, 2)]
    reply_markup = {"keyboard": keyboard, "one_time_keyboard": True}
    return json.dumps(reply_markup)


# endregion keyboard helper functions

def main():
    # populate the dicts for questions and pages
    global questions, pages
    root_path = Path(__file__).parent.parent
    with open(root_path / 'questionids.txt', 'r') as inf:
        for line in inf:
            questions = eval(line)

    for key, value in questions.items():
        pages[key] = str(gform.get_page_number(value))

    # Add the daily night reset
    scheduler.run_daily(daily_night_reset, datetime.time(hour=23, minute=55, tzinfo=TIMEZONE), name="daily_night_reset")

    # Send get request every 15min to prevent sleeping
    # Two jobs are scheduled each at 30min interval in case one fails
    if MODE == "prod":
        scheduler.run_repeating(do_not_sleep, 1800, first=60, name="do_not_sleep")
        scheduler.run_repeating(do_not_sleep, 1800, first=960, name="do_not_sleep_2")

    # on different commands - answer in Telegram
    convo_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("config", config),
            CommandHandler("force_submit", force_submit),
            CommandHandler("stop", stop),
            MessageHandler(Filters.text(TEMPERATURES), set_temperature)
        ],
        states={
            STATE_SET_GROUP: [MessageHandler(Filters.text, set_group)],
            STATE_SET_NAME: [MessageHandler(Filters.text, set_name)],
            STATE_SET_REMINDER_AM: [MessageHandler(Filters.text, set_reminder_am)],
            STATE_SET_REMINDER_PM: [MessageHandler(Filters.text, set_reminder_pm)],
            STATE_SET_TEMPERATURE_AM: [MessageHandler(Filters.text, set_temperature_am)],
            STATE_SET_TEMPERATURE_PM: [MessageHandler(Filters.text, set_temperature_pm)]
        },
        fallbacks=[CommandHandler("stop", stop)],
        allow_reentry=True
    )
    dispatcher.add_handler(convo_handler)
    dispatcher.add_error_handler(error)

    if MODE == "dev":
        updater.start_polling()
    elif MODE == "prod":
        # Start the Bot
        updater.start_webhook(listen="0.0.0.0",
                              port=PORT,
                              url_path=TOKEN,
                              webhook_url="https://{}.herokuapp.com/{}".format(APP_NAME, TOKEN))
        # updater.bot.set_webhook(url=settings.WEBHOOK_URL)
        # updater.bot.set_webhook("https://{}.herokuapp.com/{}".format(APP_NAME, TOKEN))
        updater.idle()
    else:
        logger.error("No MODE specified!")


if __name__ == '__main__':
    main()
