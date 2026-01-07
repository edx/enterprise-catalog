"""
Tests for translation utilities.
"""
from unittest.mock import patch

import requests
from django.test import TestCase

from enterprise_catalog.apps.catalog.translation_utils import (
    translate_object_fields,
    translate_to_spanish,
)


class TranslateToSpanishTests(TestCase):
    """
    Tests for the translate_to_spanish function.
    """

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_success(self, mock_chat_completion):
        """Test successful translation."""
        mock_chat_completion.return_value = 'Hola Mundo'

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, 'Hola Mundo')
        mock_chat_completion.assert_called_once()

        # Verify the call arguments
        call_args = mock_chat_completion.call_args
        self.assertIn('system_message', call_args.kwargs)
        self.assertIn('user_messages', call_args.kwargs)
        self.assertEqual(len(call_args.kwargs['user_messages']), 1)
        self.assertEqual(call_args.kwargs['user_messages'][0]['role'], 'user')
        self.assertIn('Hello World', call_args.kwargs['user_messages'][0]['content'])

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_empty_string(self, mock_chat_completion):
        """Test translation with empty string."""
        result = translate_to_spanish('')

        self.assertEqual(result, '')
        mock_chat_completion.assert_not_called()

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_none_value(self, mock_chat_completion):
        """Test translation with None value."""
        result = translate_to_spanish(None)

        self.assertEqual(result, '')
        mock_chat_completion.assert_not_called()

    @patch('enterprise_catalog.apps.catalog.translation_utils.LOGGER')
    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_value_error(self, mock_chat_completion, mock_logger):
        """Test translation handles ValueError gracefully."""
        mock_chat_completion.side_effect = ValueError('API error')

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, '')
        mock_logger.error.assert_called_once()
        # Verify error logging includes the error message
        log_call_args = mock_logger.error.call_args[0]
        self.assertIn('Error translating text', log_call_args[0])

    @patch('enterprise_catalog.apps.catalog.translation_utils.LOGGER')
    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_http_error(self, mock_chat_completion, mock_logger):
        """Test translation handles HTTPError gracefully."""
        mock_chat_completion.side_effect = requests.exceptions.HTTPError('502 Server Error')

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, '')
        mock_logger.error.assert_called_once()
        # Verify error logging includes the error message
        log_call_args = mock_logger.error.call_args[0]
        self.assertIn('API error translating text', log_call_args[0])

    @patch('enterprise_catalog.apps.catalog.translation_utils.LOGGER')
    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_general_exception(self, mock_chat_completion, mock_logger):
        """Test translation handles general exceptions gracefully."""
        mock_chat_completion.side_effect = RuntimeError('Unexpected error')

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, '')
        mock_logger.error.assert_called_once()
        # Verify error logging includes the error message
        log_call_args = mock_logger.error.call_args[0]
        self.assertIn('Unexpected error translating text', log_call_args[0])

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_empty_response(self, mock_chat_completion):
        """Test translation when chat_completion returns empty string."""
        mock_chat_completion.return_value = ''

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, '')

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_none_response(self, mock_chat_completion):
        """Test translation when chat_completion returns None."""
        mock_chat_completion.return_value = None

        result = translate_to_spanish('Hello World')

        self.assertEqual(result, '')

    @patch('enterprise_catalog.apps.catalog.translation_utils.settings')
    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_custom_system_message(self, mock_chat_completion, mock_settings):
        """Test that custom system message from settings is used."""
        custom_message = 'Custom translation system message'
        mock_settings.SPANISH_TRANSLATION_SYSTEM_MESSAGE = custom_message
        mock_chat_completion.return_value = 'Hola'

        translate_to_spanish('Hello')

        call_args = mock_chat_completion.call_args
        self.assertEqual(call_args.kwargs['system_message'], custom_message)

    @patch('enterprise_catalog.apps.catalog.translation_utils.chat_completion')
    def test_translate_to_spanish_long_text(self, mock_chat_completion):
        """Test translation with long text (for logging truncation)."""
        long_text = 'A' * 200
        mock_chat_completion.side_effect = ValueError('Error')

        result = translate_to_spanish(long_text)

        self.assertEqual(result, '')


class TranslateObjectFieldsTests(TestCase):
    """
    Tests for the translate_object_fields function.
    """

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_single_field(self, mock_translate):
        """Test translating a single field."""
        mock_translate.return_value = 'Título Traducido'

        data = {'title': 'Test Title', 'other_field': 'Not Translated'}
        fields = ['title']

        result = translate_object_fields(data, fields)

        self.assertEqual(result['title'], 'Título Traducido')
        self.assertEqual(result['other_field'], 'Not Translated')
        mock_translate.assert_called_once_with('Test Title')

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_multiple_fields(self, mock_translate):
        """Test translating multiple fields."""
        mock_translate.side_effect = [
            'Título Traducido',
            'Descripción Traducida',
            'Subtítulo Traducido'
        ]

        data = {
            'title': 'Test Title',
            'description': 'Test Description',
            'subtitle': 'Test Subtitle',
            'unchanged': 'Should Not Change'
        }
        fields = ['title', 'description', 'subtitle']

        result = translate_object_fields(data, fields)

        self.assertEqual(result['title'], 'Título Traducido')
        self.assertEqual(result['description'], 'Descripción Traducida')
        self.assertEqual(result['subtitle'], 'Subtítulo Traducido')
        self.assertEqual(result['unchanged'], 'Should Not Change')
        self.assertEqual(mock_translate.call_count, 3)

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_missing_field(self, mock_translate):
        """Test translating when field doesn't exist in data."""
        data = {'title': 'Test Title'}
        fields = ['title', 'nonexistent_field']

        result = translate_object_fields(data, fields)

        self.assertEqual(result['title'], mock_translate.return_value)
        self.assertNotIn('nonexistent_field', result)
        # Should only translate existing fields
        mock_translate.assert_called_once_with('Test Title')

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_non_string_field(self, mock_translate):
        """Test that non-string fields are not translated."""
        data = {
            'title': 'Test Title',
            'count': 42,
            'is_active': True,
            'items': ['item1', 'item2']
        }
        fields = ['title', 'count', 'is_active', 'items']

        result = translate_object_fields(data, fields)

        # Only string fields should be translated
        self.assertEqual(result['title'], mock_translate.return_value)
        self.assertEqual(result['count'], 42)
        self.assertEqual(result['is_active'], True)
        self.assertEqual(result['items'], ['item1', 'item2'])
        mock_translate.assert_called_once_with('Test Title')

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_empty_data(self, mock_translate):
        """Test translating empty data dictionary."""
        data = {}
        fields = ['title', 'description']

        result = translate_object_fields(data, fields)

        self.assertEqual(result, {})
        mock_translate.assert_not_called()

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_empty_fields_list(self, mock_translate):
        """Test translating with empty fields list."""
        data = {'title': 'Test Title', 'description': 'Test Description'}
        fields = []

        result = translate_object_fields(data, fields)

        self.assertEqual(result, data)
        mock_translate.assert_not_called()

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_original_data_unchanged(self, mock_translate):
        """Test that original data dictionary is not modified."""
        mock_translate.return_value = 'Título Traducido'

        original_data = {'title': 'Test Title', 'description': 'Test Description'}
        fields = ['title']

        result = translate_object_fields(original_data, fields)

        # Original data should remain unchanged
        self.assertEqual(original_data['title'], 'Test Title')
        # Result should have translated data
        self.assertEqual(result['title'], 'Título Traducido')

    @patch('enterprise_catalog.apps.catalog.translation_utils.translate_to_spanish')
    def test_translate_object_fields_with_empty_strings(self, mock_translate):
        """Test translating fields with empty string values."""
        mock_translate.return_value = ''

        data = {'title': '', 'description': 'Test Description'}
        fields = ['title', 'description']

        translate_object_fields(data, fields)

        # translate_to_spanish should be called for both fields
        self.assertEqual(mock_translate.call_count, 2)
