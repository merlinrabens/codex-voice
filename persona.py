"""Small, optional host-owned persona configuration for voice and task identity."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_persona(path=None, name=None):
    profile = {}
    if path is not None:
        source = Path(path).expanduser().resolve()
        if source == ROOT or ROOT in source.parents:
            raise ValueError('Keep private assistant profiles outside the source checkout.')
        raw = source.read_bytes()
        if len(raw) > 16000:
            raise ValueError('Assistant profiles must be at most 16000 bytes.')
        profile = json.loads(raw)
        if not isinstance(profile, dict) or set(profile) - {'name', 'persona_age', 'instructions'}:
            raise ValueError('Assistant profiles support only name, persona_age, and instructions.')
    selected_name = name if name is not None else profile.get('name', 'Astra')
    if (not isinstance(selected_name, str) or not selected_name.strip()
            or len(selected_name) > 64 or not selected_name.isprintable()):
        raise ValueError('Assistant names must contain 1 to 64 printable characters.')
    age = profile.get('persona_age')
    if age is not None and (type(age) is not int or not 0 <= age <= 120):
        raise ValueError('Persona age must be an integer from 0 to 120.')
    instructions = profile.get('instructions', '')
    if not isinstance(instructions, str) or len(instructions) > 6000:
        raise ValueError('Assistant instructions must be text of at most 6000 characters.')
    return {'name': selected_name.strip(), 'persona_age': age, 'instructions': instructions.strip()}


def persona_instructions(profile):
    parts = [f'Your assistant persona in this client is named {json.dumps(profile["name"], ensure_ascii=False)}. '
             'Use that name consistently in conversation and when relaying task results. '
             'Your name is distinct from the underlying model or software product.']
    if profile.get('persona_age') is not None:
        parts.append(f'Your configured persona age is {profile["persona_age"]}. '
                     'This is a persona attribute, not a literal human age or birth history.')
    parts.append('You are an AI assistant. Do not invent human experiences, memories, '
                 'tool access, or completed actions. Follow the existing task and permission instructions.')
    if profile.get('instructions'):
        parts.append(profile['instructions'])
    return '\n\n'.join(parts)
