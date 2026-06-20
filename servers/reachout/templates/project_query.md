Subject: Quick question about {{ company }}{% if topic %} — {{ topic }}{% endif %}

Hi {{ name | default("there") }},

I'm {{ sender | default("Naman Sharma") }}, a developer following {{ company }}. {{ question | default("I had a quick question about your product direction.") }}

{% if pitch %}On a related note, here's something I think could help:

{{ pitch }}
{% endif %}
Would appreciate any pointers — thanks for your time.

Best,
{{ sender | default("Naman Sharma") }}
{{ signature | default("") }}
