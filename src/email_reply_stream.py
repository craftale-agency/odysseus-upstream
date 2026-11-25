"""Stream only the explicitly delimited email body, never model reasoning."""
import json
import logging
import re

logger = logging.getLogger(__name__)

# A mangled closing marker: the model emitted <<<END>>> but a middlebox
# between us and the model deleted the inner "<END>" substring. The
# craftshost langfuse-proxy scrubs gemma4 composer channel sentinels with
# a regex whose pipes are optional (r"<\|?(?:channel|end)\|?>"), so a plain
# "<END>" matches too and <<<END>>> arrives as bare angle brackets
# (observed live: "<<>>" and "<<>"). The reply between the markers is
# complete and usable, so recognise the leftover bracket run and keep it.
_MANGLED_END_RE = re.compile(r'[<>]{2,}\s*$')


def reply_body(raw, *, complete=False):
    match = re.search(r'<<<\s*REPLY\s*>>>', raw, re.I)
    if not match:
        return ''
    body = raw[match.end():]
    end = re.search(r'<<<\s*END\s*>>>', body, re.I)
    if end:
        body = body[:end.start()]
    elif complete:
        # No END marker. If the body ends in a bare angle-bracket run that
        # is a mangled closer (see _MANGLED_END_RE), the reply itself is
        # finished — strip the debris and accept it. Without a recognisable
        # closer the stream is genuinely incomplete (budget truncation, or
        # the model never closed the block): keep the strict behaviour so
        # analysis-only or cut-off output is still rejected.
        salvaged = _MANGLED_END_RE.sub('', body)
        if salvaged == body:
            return ''
        body = salvaged
    else:
        body = body.split('<', 1)[0]
    if re.search(r'</?think\b', body, re.I):
        return ''
    return body.strip()


async def stream_reply(candidates, messages, emit, *, max_tokens=1536):
    from src.llm_core import stream_llm
    error = 'No usable reply returned'
    for url, model, headers in candidates:
        raw = ''
        visible = ''
        await emit({'type': 'reply', 'text': ''})
        try:
            async for chunk in stream_llm(url, model, messages, headers=headers,
                    temperature=0.3, max_tokens=max_tokens, timeout=120, thinking_mode='off'):
                for line in chunk.splitlines():
                    if not line.startswith('data: ') or line[6:] == '[DONE]':
                        continue
                    event = json.loads(line[6:])
                    if event.get('error'):
                        raise RuntimeError(event['error'])
                    if event.get('thinking'):
                        continue
                    raw += event.get('delta') or ''
                    body = reply_body(raw)
                    if body != visible:
                        visible = body
                        await emit({'type': 'reply', 'text': body})
            if not reply_body(raw, complete=True):
                raise ValueError('Model returned analysis or an incomplete reply, not a finished email')
            if not re.search(r'<<<\s*END\s*>>>', raw, re.I):
                # Accepted via mangled-closer salvage: the reply is fine, but
                # something on this lane is still eating the literal END
                # marker — surface it so the middlebox gets fixed.
                logger.warning(
                    "email reply salvaged from mangled END marker model=%s raw_tail=%r",
                    model, raw[-60:],
                )
            return raw, model
        except Exception as exc:
            error = str(exc)
            logger.warning(
                "email reply candidate rejected model=%s raw_len=%s reply_marker=%s end_marker=%s raw_tail=%r",
                model, len(raw),
                bool(re.search(r'<<<\s*REPLY\s*>>>', raw, re.I)),
                bool(re.search(r'<<<\s*END\s*>>>', raw, re.I)),
                raw[-60:],
            )
            await emit({'type': 'reply', 'text': ''})
    raise ValueError(error)
