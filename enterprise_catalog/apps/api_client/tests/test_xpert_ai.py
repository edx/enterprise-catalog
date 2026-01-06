"""
Tests for Xpert AI client.
"""
import json
from unittest.mock import patch

import requests
from django.conf import settings
from django.test import TestCase

from enterprise_catalog.apps.api_client.xpert_ai import (
    CONNECT_TIMOUET_SECONDS,
    READ_TIMEOUT_SECONDS,
    chat_completion,
)


class ChatCompletionTests(TestCase):
    """
    Tests for the chat_completion function.
    """

    def setUp(self):
        self.system_message = "Test system message"
        self.user_messages = [{"role": "user", "content": "Test user message"}]

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_success(self, mock_post):
        """Test successful chat completion."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = [{"content": "Test response"}]

        result = chat_completion(self.system_message, self.user_messages)

        self.assertEqual(result, "Test response")
        mock_post.assert_called_once_with(
            settings.XPERT_AI_API_V2,
            headers={'Content-Type': 'application/json'},
            data=json.dumps({
                'client_id': settings.XPERT_AI_CLIENT_ID,
                'system_message': self.system_message,
                'messages': self.user_messages,
            }),
            timeout=(CONNECT_TIMOUET_SECONDS, READ_TIMEOUT_SECONDS)
        )

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_http_error(self, mock_post):
        """Test that HTTP errors raise an exception."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("Internal Server Error")

        with self.assertRaises(requests.exceptions.HTTPError):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_invalid_json(self, mock_post):
        """Test handling of invalid JSON response."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.side_effect = ValueError("No JSON object could be decoded")

        with self.assertRaisesRegex(ValueError, "Invalid JSON response from Xpert AI"):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_invalid_format(self, mock_post):
        """Test handling of non-list response format."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"not": "a list"}

        with self.assertRaisesRegex(ValueError, "Invalid response format from Xpert AI"):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_api_error_message(self, mock_post):
        """Test handling of API error messages."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = [{"message": "Rate limit exceeded"}]

        with self.assertRaisesRegex(ValueError, "Xpert AI returned error: Rate limit exceeded"):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_missing_content(self, mock_post):
        """Test handling of missing content field."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = [{"not_content": "oops"}]

        with self.assertRaisesRegex(ValueError, "Xpert AI response missing content field"):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_empty_list_response(self, mock_post):
        """Test handling of empty list response."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = []

        with self.assertRaisesRegex(ValueError, "Invalid response format from Xpert AI"):
            chat_completion(self.system_message, self.user_messages)

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_both_message_and_content(self, mock_post):
        """Test that content is extracted even if message field is present."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = [{
            "message": "Some info message",
            "content": "Actual content"
        }]

        result = chat_completion(self.system_message, self.user_messages)

        self.assertEqual(result, "Actual content")

    @patch('enterprise_catalog.apps.api_client.xpert_ai.requests.post')
    def test_chat_completion_empty_string_content(self, mock_post):
        """Test that empty string content is allowed."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = [{"content": ""}]

        result = chat_completion(self.system_message, self.user_messages)

        self.assertEqual(result, "")
