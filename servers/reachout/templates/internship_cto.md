Subject: A project idea for {{ company }}{% if name %} — for {{ name }}{% endif %}

Hi {{ name | default("there") }},

I came across {{ company }}{% if round %} after your {{ round }} round{% endif %} and {{ reason | default("really liked what you're building") }}.

I'm {{ sender | default("Naman Sharma") }}, an early-career software developer. Rather than a generic note, I sketched something concrete I could build for {{ company }}:

{{ pitch }}

If that's useful, I'd love to build a quick prototype or chat for 15 minutes. Happy to send a one-pager.

Best,
{{ sender | default("Naman Sharma") }}
{{ signature | default("") }}
