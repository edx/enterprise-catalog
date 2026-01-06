"""
Xpert AI client
"""

import json

import requests
from django.conf import settings


CONNECT_TIMOUET_SECONDS = 5
READ_TIMEOUT_SECONDS = 20


def chat_completion(system_message, user_messages):
    """
    Generate response using xpert api.

    Arguments:
        system_message (str): System message to be sent to the API.
        user_messages (list): List of user messages to be sent to the API.

    Returns:
        (str): Prompt response from Xpert AI.
    """
    headers = {
        'Content-Type': 'application/json',
    }

    body = {
        'client_id': settings.XPERT_AI_CLIENT_ID,
        'system_message': system_message,
        'messages': user_messages,
    }

    response = requests.post(
        settings.XPERT_AI_API_V2,
        headers=headers,
        data=json.dumps(body),
        timeout=(CONNECT_TIMOUET_SECONDS, READ_TIMEOUT_SECONDS)
    )
    response.raise_for_status()

    # Validate response
    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError) as ex:
        raise ValueError(f'Invalid JSON response from Xpert AI: {str(ex)}') from ex

    # Check if response is empty or not a list
    if response_data is None or not isinstance(response_data, list) or not response_data:
        # Truncate large response data for logging
        truncated_data = str(response_data)[:100]
        raise ValueError(f'Invalid response format from Xpert AI: {truncated_data}')

    # Check if the first element has an error message instead of content
    first_element = response_data[0]
    if 'message' in first_element and 'content' not in first_element:
        error_message = first_element.get('message', 'Unknown error')
        raise ValueError(f'Xpert AI returned error: {error_message}')

    # Extract content from the response
    response_content = first_element.get('content')
    if response_content is None:
        raise ValueError('Xpert AI response missing content field')

    return response_content
