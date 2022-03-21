# -*- coding: utf-8 -*-
import requests
import json
import logging
import urllib.request

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(funcName)s() - %(lineno)d - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class GFormHelper:
    """
    Helper class with functions to scrape data from a Google Form and to submit the form

    Attributes
    --------------------------
    url : str
        the url for the Google Form
    form_data : list
        contains data for all questions in the form

    """

    QUESTION_TYPE_IDS = [0, 1, 2, 3, 4, 5, 7, 9, 10]  # type_ids for all question types
    QUESTION_OPTIONS_TYPE_IDS = [2, 3, 4]  # type_ids for multiple-choice, drop-down, checkbox questions

    SECTION_HEADER_TYPE_ID = 8  # type_id of section header

    def __init__(self, url):
        """
        initialises an object with a given form url and populates self.form_data

        :param url: Google Form url (must end in '/viewform' or '/formResponse'
        """
        self.url = url.replace("viewform", "formResponse")
        # self.form_data is set in update_list
        self.update_list()

    def get_first_question_id(self):
        """
        gets the id of the first question

        :return: question_id of first question
        """
        for element in self.form_data:
            if len(element) > 4:
                if element[3] in self.QUESTION_TYPE_IDS:
                    if isinstance(element[4], list):
                        return element[4][0][0]

    def get_options(self, question_id):
        """
        gets a list of options given a question id

        :param question_id: target id
        :return: list of options in the target question or [] if no options available
        """
        for element in self.form_data:
            # Check if element is a type of question that contains selectable options
            if len(element) > 4 and element[3] in self.QUESTION_OPTIONS_TYPE_IDS:
                if isinstance(element[4], list) and element[4][0][0] == question_id:
                    # list of options excluding 'Other' option
                    return list(filter(lambda x: x != '', map(lambda x: x[0], element[4][0][1])))
        return []

    def get_options_and_next_question_id(self, question_id):
        """
        gets a dict of option: next_question_id given a question id

        :param question_id: target id
        :return: dict of option: next_question_id or {} if no options available
        """
        options_dict = {}
        for element in self.form_data:
            # Check if element is a type of question that contains selectable options
            if len(element) > 4 and element[3] in self.QUESTION_OPTIONS_TYPE_IDS:
                if isinstance(element[4], list) and element[4][0][0] == question_id:
                    # dict of {option: next_section_id}
                    options_dict = dict(filter(lambda x: x[0] != '', map(lambda x: [x[0], x[2]], element[4][0][1])))

        # For each option find the target section header
        # Then return the question_id of the first question after the section header
        for option in options_dict:
            in_section = False
            for element in self.form_data:
                if len(element) > 0 and element[0] == options_dict[option]:
                    # reached the correct section header
                    in_section = True
                elif in_section and len(element) > 4:
                    if element[3] in self.QUESTION_TYPE_IDS:
                        if isinstance(element[4], list):
                            options_dict[option] = element[4][0][0]
                            break
                    else:
                        logger.warning(
                            "Non-question wrongly identified as question OR unknown question type found : " + str(
                                element))

        return options_dict

    def get_page_number(self, element_id):
        """
        gets the page number (which is required for form submission) of a given element

        :param element_id: target id
        :return: page number of element (note: first page starts from 0)
        """
        current_page = 0
        for element in self.form_data:
            if len(element) >= 4 and element[3] == self.SECTION_HEADER_TYPE_ID:
                # reached a new section, add to page counter
                current_page += 1

            elif len(element) > 0 and element[0] == element_id:
                # check if element id matches
                return current_page

            elif len(element) > 4 and element[3] in self.QUESTION_TYPE_IDS:
                # this case is True if the given parameter is an input_id of a question
                if isinstance(element[4], list) and element[4][0][0] == element_id:
                    return current_page

        logger.warning("Element with id: {}  not found! ".format(element_id))
        return 0

    def update_list(self):
        """
        updates the class attribute self.form_data based on the self.url initialised during object creation
        """
        url_data = urllib.request.urlopen(self.url)
        url_str = url_data.read().decode("utf8")

        # All important form data (incl questions and options if any) is within this excerpt
        url_str = url_str.split("FB_PUBLIC_LOAD_DATA_ = ", 1)[1].split(";</script>", 1)[0].strip()
        url_data.close()

        # Question data is located 2 layers deep
        # The first layer contains form name and other misc settings
        # The second layer contains form title, form description and question data
        self.form_data = json.loads(url_str)[1][1]
        logger.info(self.form_data)

    def submit_form(self, data):
        """
        sends a POST request to submit the form with given data

        :param data: dict of values required for submission
                    { entry.<id> : <response>, ... , pageHistory : "<page_num1>, <page_num2>, ... " }
        :return: dict of option: next_question_id or {} if no options available
        """
        response = requests.post(self.url, data)
        logger.info(response.status_code)
        return response.status_code
