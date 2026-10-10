"""Ask the ERP a question in plain language.

The loop is small and deliberately boring:

    question + tool list  ->  Claude
    Claude: "call site_visit_stats(2026-09-01, 2026-09-30)"
    us:     run it, scoped to the asking user, hand back counts
    Claude: the sentence the user reads

Claude chooses *which* numbers to fetch; this code decides *what it is allowed
to fetch and for whom* (see tools.py). Nothing but counts crosses the wire, so
no client name, phone number or address reaches the API.

Uses `requests` rather than the anthropic SDK on purpose: the Messages API is one
POST, requests is already a dependency, and the deployment does not have to grow
a package for this.
"""
import json
import logging
import os

import requests
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .tools import TOOL_SPECS, run_tool

logger = logging.getLogger(__name__)

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'
# Picking a tool and summarising a handful of counts does not need a large model,
# and this one answers in about a second for a fraction of a cent.
MODEL = 'claude-haiku-4-5-20251001'
MAX_TOKENS = 1024
# Each pass is one Claude call. Two tools plus the write-up fits comfortably;
# the cap exists so a model that keeps asking for tools cannot bill in a loop.
MAX_PASSES = 5
TIMEOUT = 30

SYSTEM = """You answer questions about a real-estate CRM, for staff of one company.

Today is {today}. Resolve relative dates against it — "last month" means the whole
of the previous calendar month, "this week" the current Monday to today.

Use the tools for every number. Never estimate, never carry a figure over from
earlier in the conversation, and never state a number no tool returned.

Answer in two or three short sentences of plain prose. Lead with the figure asked
for, then at most a couple of notable splits. No preamble, no bullet lists, no
markdown headings. If a tool returns zero rows, say plainly that there were none
in that period.

You only have company-wide aggregates. If asked for an individual client, a phone
number, or anything about a specific person, say that you can only report totals
and suggest the relevant screen instead."""


def _key():
    return (os.getenv('ANTHROPIC_API_KEY') or '').strip()


class AskView(APIView):
    """POST {"question": "..."} -> {"answer": "...", "used": [...]}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        question = (request.data.get('question') or '').strip()
        if not question:
            return Response({'detail': 'Ask a question.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(question) > 500:
            return Response({'detail': 'That question is too long.'},
                            status=status.HTTP_400_BAD_REQUEST)
        key = _key()
        if not key:
            # A missing key is a deployment state, not a bug — say so plainly rather
            # than 500ing, so the screen can hide itself instead of looking broken.
            return Response({'detail': 'The assistant is not configured on this server.'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from django.utils import timezone
        messages = [{'role': 'user', 'content': question}]
        used = []

        for _ in range(MAX_PASSES):
            try:
                reply = self._call(key, messages, timezone.localdate())
            except requests.Timeout:
                return Response({'detail': 'The assistant took too long. Try again.'},
                                status=status.HTTP_504_GATEWAY_TIMEOUT)
            except Exception:
                logger.exception('AI ask failed')
                return Response({'detail': 'The assistant is unavailable right now.'},
                                status=status.HTTP_502_BAD_GATEWAY)

            if reply.get('stop_reason') != 'tool_use':
                return Response({'answer': _text_of(reply), 'used': used})

            messages.append({'role': 'assistant', 'content': reply.get('content', [])})
            results = []
            for block in reply.get('content', []):
                if block.get('type') != 'tool_use':
                    continue
                out = run_tool(block.get('name'), block.get('input') or {}, request.user)
                used.append(block.get('name'))
                results.append({
                    'type': 'tool_result',
                    'tool_use_id': block.get('id'),
                    'content': json.dumps(out, default=str),
                })
            if not results:
                return Response({'answer': _text_of(reply), 'used': used})
            messages.append({'role': 'user', 'content': results})

        # Ran out of passes with the model still asking for tools.
        return Response({'detail': 'The assistant could not finish that one. Try asking '
                                   'for one thing at a time.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    def _call(self, key, messages, today):
        res = requests.post(
            API_URL,
            headers={'x-api-key': key, 'anthropic-version': API_VERSION,
                     'content-type': 'application/json'},
            json={'model': MODEL, 'max_tokens': MAX_TOKENS,
                  'system': SYSTEM.format(today=today.isoformat()),
                  'tools': TOOL_SPECS, 'messages': messages},
            timeout=TIMEOUT,
        )
        res.raise_for_status()
        return res.json()


def _text_of(reply):
    parts = [b.get('text', '') for b in reply.get('content', []) if b.get('type') == 'text']
    return '\n'.join(p for p in parts if p).strip() or 'No answer.'


class AiStatusView(APIView):
    """Whether the assistant is usable, so the UI can hide the box rather than
    offer something that will only ever return 503."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'enabled': bool(_key())})
