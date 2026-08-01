"""
Generate a Homeric ode for a self-hosted Roll Call based on ranked attestations
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from datetime import date, timedelta
from rollcall.models import WeeklyRollCall, Attestation, RankingTrial


ODE_PROMPT_TEMPLATE = """You are the oracle of fitness. You speak like Homer recounting the battle in the Iliad. You are recruiting an army for a great battle. You've been given health attestations they have provided and ranked the participants. You prioritized for battle-readiness and judged the quality and intensity underlying reported feats.

After much work, you have already found the top {count} applicants (selected out of thousands, so there is no "shame" in last place). You ranked them in this order (top to bottom):

{rankings}

You are now to create a RHYMING ode to the top {count}, uplifting each, around 1000 words, starting from {count_ordinal} place and building to first.

There are exactly {count} warriors — do NOT reference ten or any other number unless it matches exactly {count}.

CRITICAL REQUIREMENT — RHYMING:
The entire ode MUST be written in rhyming couplets (AA BB CC). Every pair of consecutive lines must rhyme. This is the single most important structural requirement. Do NOT write prose poetry or blank verse — it MUST rhyme.

Example of the required style (showing rhyming couplets with specific details):

**EIGHTH AMONG HEROES: ALICE OF THE ROSENGARDEN**

From distant lands where roses bloom in spring,
Comes alice_rozengarden, whose praises I sing!
With elastic bands she conquered curl and lift,
A portable arsenal, her ingenious gift!

Through thirty thousand steps she marched with might,
From Monday's ten thousand to walls pushed upright,
With hollow body holds and planks held strong,
Her superman pose lasted battle-long!

Additional notes:
- Give each warrior a bold header and 3-4 stanzas of 4 lines each (2 rhyming couplets per stanza)
- Reference specific details from each warrior's attestation (exercises, weights, distances, etc.)
- Make it epic and Homeric in style
- Build dramatic tension as you ascend the rankings
- Celebrate each warrior's unique contributions
- End with a bold closing chorus, also in rhyming couplets

Attestations:

{attestations}
"""


def ordinal(n):
    """Return ordinal string for an integer (e.g. 1 -> '1st', 8 -> '8th')."""
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def add_trailing_spaces(text):
    """Add two trailing spaces to verse lines for markdown line breaks."""
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.rstrip()
        if (stripped
                and not stripped.startswith('#')
                and not stripped.startswith('---')
                and not (stripped.startswith('**') and stripped.endswith('**'))
                and not (stripped.startswith('*') and stripped.endswith('*') and not stripped.startswith('**'))):
            result.append(stripped + '  ')
        else:
            result.append(line)
    return '\n'.join(result)


class Command(BaseCommand):
    help = 'Generate a Homeric ode for a self-hosted Roll Call'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week-end',
            type=str,
            help='Week end date (YYYY-MM-DD). Defaults to most recent Sunday.'
        )
        parser.add_argument(
            '--output',
            type=str,
            help='Output file path (optional, defaults to stdout)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show prompt without calling API'
        )
        parser.add_argument(
            '--provider',
            type=str,
            choices=['anthropic', 'deepseek'],
            default='anthropic',
            help='AI provider to use (default: anthropic)'
        )

    def handle(self, *args, **options):
        # Determine week
        if options['week_end']:
            week_end = date.fromisoformat(options['week_end'])
        else:
            # Find most recent Sunday
            today = date.today()
            days_since_sunday = (today.weekday() + 1) % 7
            week_end = today - timedelta(days=days_since_sunday)

        # Get roll call
        try:
            roll_call = WeeklyRollCall.objects.get(week_end_date=week_end)
        except WeeklyRollCall.DoesNotExist:
            self.stderr.write(f"No roll call found for week ending {week_end}")
            return

        self.stdout.write(f"Week: {roll_call.week_start_date} to {roll_call.week_end_date}")

        # Get aggregated rankings from trials (uses name merging to avoid splits)
        trials = RankingTrial.objects.filter(weekly_roll_call=roll_call)
        if not trials.exists():
            self.stderr.write("No ranking trials found. Run ranking trials first.")
            return

        from rollcall.services.ranking_stats import calculate_cumulative_stats

        stats = calculate_cumulative_stats(trials)
        sorted_stats = sorted(stats.items(), key=lambda x: x[1]['average_rank'])

        avg_rankings = []
        for name, data in sorted_stats:
            avg_rankings.append({
                'name': name,
                'avg_rank': data['average_rank'],
                'std_error': data['std_error'],
                'trials': data['trial_count']
            })

        # Merge name fragments: if "Devin" (1 trial) is a substring of
        # "Devin | GammaSwap" (13 trials), merge the orphan into the longer name.
        merged = []
        skip_indices = set()
        for i, entry in enumerate(avg_rankings):
            if i in skip_indices:
                continue
            for j, other in enumerate(avg_rankings):
                if j == i or j in skip_indices:
                    continue
                # If one name is a prefix/substring of the other, merge the smaller trial count into the larger
                i_norm = entry['name'].lower().strip()
                j_norm = other['name'].lower().strip()
                if (i_norm in j_norm or j_norm in i_norm):
                    if entry['trials'] <= other['trials']:
                        skip_indices.add(i)
                    else:
                        skip_indices.add(j)
            if i not in skip_indices:
                merged.append(entry)

        top_10 = merged[:10]

        # Build rankings text
        rankings_text = "Rank | Name | Avg Rank | Std Error | Trials\n"
        rankings_text += "-" * 50 + "\n"
        for i, r in enumerate(top_10, 1):
            rankings_text += f"{i}. {r['name']} | {r['avg_rank']:.2f} | ±{r['std_error']:.2f} | {r['trials']}\n"

        self.stdout.write(f"\nTop {len(top_10)} Rankings:\n{rankings_text}")

        # Get attestations for top 10
        attestations = Attestation.objects.filter(
            weekly_roll_call=roll_call,
            parent_attestation__isnull=True
        ).select_related('discord_user', 'telegram_user')

        attestations_text = ""
        top_10_names = [r['name'] for r in top_10]

        for att in attestations:
            name = att.user_mapping.linked_name if att.user_mapping else None
            if not name:
                continue
            if name not in top_10_names:
                continue

            # Get all parts
            from django.db.models import Q
            parts = list(Attestation.objects.filter(
                Q(id=att.id) | Q(parent_attestation=att)
            ).order_by('part_number'))

            full_text = '\n'.join([p.raw_text for p in parts])
            attestations_text += f"\n{'='*60}\n{name}:\n{'='*60}\n{full_text}\n"

        # Build prompt
        count = len(top_10)
        prompt = ODE_PROMPT_TEMPLATE.format(
            rankings=rankings_text,
            attestations=attestations_text,
            count=count,
            count_ordinal=ordinal(count)
        )

        if options['dry_run']:
            self.stdout.write("\n" + "="*60)
            self.stdout.write("DRY RUN - Prompt that would be sent:")
            self.stdout.write("="*60)
            self.stdout.write(prompt)
            self.stdout.write("="*60)
            return

        provider = options['provider']
        if provider == 'deepseek':
            self._generate_with_deepseek(prompt, options)
        else:
            self._generate_with_anthropic(prompt, options)

    def _generate_with_anthropic(self, prompt, options):
        self.stdout.write("\nGenerating Homeric ode with Claude...")
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            message = client.messages.create(
                model='claude-sonnet-4-20250514',
                max_tokens=4096,
                messages=[{'role': 'user', 'content': prompt}]
            )
            ode = add_trailing_spaces(message.content[0].text)
            self.stdout.write(f"\nTokens used: {message.usage.input_tokens} input, {message.usage.output_tokens} output")
            self._output_ode(ode, options)
        except Exception as e:
            self.stderr.write(f"Error calling Claude: {e}")
            raise

    def _generate_with_deepseek(self, prompt, options):
        self.stdout.write("\nGenerating Homeric ode with DeepSeek...")
        try:
            import openai
            client = openai.OpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url="https://api.deepseek.com/v1"
            )
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are the oracle of fitness, a poet of epic battle and warrior glory. You ALWAYS write in rhyming couplets — every pair of consecutive lines must rhyme (AA BB CC pattern). Never write prose or blank verse."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4096
            )
            ode = add_trailing_spaces(response.choices[0].message.content)
            if hasattr(response, 'usage'):
                self.stdout.write(f"\nTokens used: {response.usage.prompt_tokens} input, {response.usage.completion_tokens} output")
            self._output_ode(ode, options)
        except Exception as e:
            self.stderr.write(f"Error calling DeepSeek: {e}")
            raise

    def _output_ode(self, ode, options):
        if options['output']:
            with open(options['output'], 'w') as f:
                f.write(ode)
            self.stdout.write(f"\nOde written to: {options['output']}")
        else:
            self.stdout.write("\n" + "="*60)
            self.stdout.write("HOMERIC ODE")
            self.stdout.write("="*60 + "\n")
            self.stdout.write(ode)
