"""
Translation utilities for enterprise catalog content.
"""
from logging import getLogger

import requests
from django.conf import settings

from enterprise_catalog.apps.api_client.xpert_ai import chat_completion


LOGGER = getLogger(__name__)


def translate_to_spanish(text):
    """
    Translate the given text to Spanish using Xpert AI.

    Args:
        text (str): The text to translate.

    Returns:
        str: The translated text in Spanish, or empty string if translation fails.
    """
    if not text:
        return ''

    # Get system message from settings, with a sensible default
    system_message = getattr(
        settings,
        'SPANISH_TRANSLATION_SYSTEM_MESSAGE',
        'You are a professional translator. Translate text to Spanish accurately '
        'while preserving formatting and meaning.'
    )

    user_message = (
        f"Translate the following text to Spanish. "
        f"Return ONLY the translated text without any comments, "
        f"explanations, or conversational responses:\n\n{text}"
    )

    messages = [
        {
            'role': 'user',
            'content': user_message
        }
    ]

    # Handle errors from chat_completion
    try:
        translated_text = chat_completion(
            system_message=system_message,
            user_messages=messages
        )
        return translated_text if translated_text else ''
    except requests.exceptions.HTTPError as ex:
        LOGGER.error(
            '[SPANISH_TRANSLATION] API error translating text: %s. Original text: %s',
            str(ex),
            text[:100]
        )
        return ''
    except ValueError as ex:
        LOGGER.error(
            '[SPANISH_TRANSLATION] Error translating text: %s. Original text: %s',
            str(ex),
            text[:100]  # Log first 100 chars to avoid excessive logging
        )
        return ''
    except Exception as ex:  # pylint: disable=broad-exception-caught
        LOGGER.error(
            '[SPANISH_TRANSLATION] Unexpected error translating text: %s',
            str(ex),
            exc_info=True
        )
        return ''


def translate_object_fields(data, fields):
    """
    Translate specified fields in a dictionary to Spanish.

    Args:
        data (dict): The dictionary containing fields to translate.
        fields (list): A list of field names to translate.

    Returns:
        dict: A new dictionary with translated fields.
    """
    translated_data = data.copy()
    for field in fields:
        if field in data and isinstance(data[field], str):
            translated_data[field] = translate_to_spanish(data[field])
    return translated_data
