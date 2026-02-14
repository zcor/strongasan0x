"""
Management command to run AI ranking trials for weekly health attestations.

Usage:
    python manage.py run_ranking_trial --week 2025-12-08  # Publication date (Monday) - uses previous week
    python manage.py run_ranking_trial --week-end 2025-12-07  # Or specify Sunday directly
    python manage.py run_ranking_trial --week 2025-12-08 --provider deepseek
    python manage.py run_ranking_trial --week 2025-12-08 --auto-continue
    python manage.py run_ranking_trial --week 2025-12-08 --dry-run  # Preview without API calls
    python manage.py run_ranking_trial --week 2025-12-08 --thinking  # Enable thinking/reasoning mode
    python manage.py run_ranking_trial --week 2025-12-08 --use-file-attachment  # Attach attestations as file
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import models
from datetime import date, timedelta, datetime
from rollcall.models import WeeklyRollCall, Attestation, RankingTrial
from rollcall.services.ai_ranking import (
    get_available_providers,
    select_random_provider,
    call_openai,
    call_anthropic,
    call_grok,
    call_deepseek,
    parse_ranking_response,
    RANKING_PROMPT
)
from rollcall.services.ranking_stats import (
    calculate_cumulative_stats,
    check_convergence,
    format_rankings_display,
    normalize_name
)
import json
import random


class Command(BaseCommand):
    help = 'Run AI ranking trial for weekly health attestations'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week-end',
            type=str,
            help='Week end date (Sunday) in YYYY-MM-DD format (defaults to most recent Sunday)',
        )
        parser.add_argument(
            '--week',
            type=str,
            help='Publication date (Monday) in YYYY-MM-DD format. If Monday, uses the previous week (that just ended). If non-Monday, calculates the Monday of that week and uses the previous week.',
        )
        parser.add_argument(
            '--provider',
            type=str,
            choices=['openai', 'anthropic', 'grok', 'deepseek'],
            help='Force specific AI provider instead of random selection',
        )
        parser.add_argument(
            '--exclude-provider',
            action='append',
            choices=['openai', 'anthropic', 'grok', 'deepseek'],
            help='Exclude one or more providers from random selection (may be passed multiple times)',
        )
        parser.add_argument(
            '--auto-continue',
            action='store_true',
            help='Automatically continue running trials until convergence',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be sent to AI providers without making API calls',
        )
        parser.add_argument(
            '--thinking',
            action='store_true',
            help='Enable thinking mode - AI will show reasoning process before ranking',
        )
        parser.add_argument(
            '--use-file-attachment',
            action='store_true',
            help='Use separate content blocks for attestations (better structure, same tokens - mainly organizational benefit)',
        )
        parser.add_argument(
            '--import-json',
            type=str,
            help='Import ranking results from a JSON file or JSON string. Can be: 1) Full format with provider/model/parsed_rankings, or 2) Simple array of names ["Name1", "Name2", ...] (use --import-provider and --import-model to specify source)',
        )
        parser.add_argument(
            '--import-provider',
            type=str,
            choices=['openai', 'anthropic', 'grok', 'deepseek'],
            help='Provider name when importing simple JSON array (used with --import-json)',
        )
        parser.add_argument(
            '--import-model',
            type=str,
            help='Model name when importing simple JSON array (used with --import-json, e.g., "grok-2")',
        )
        parser.add_argument(
            '--output-ranked-attestations',
            type=str,
            help='Output file path to write attestations in final ranked order (e.g., "ranked_attestations.txt")',
        )
        parser.add_argument(
            '--output-only',
            action='store_true',
            help='Display aggregated ranking results from existing trials without running new trials. Optionally use with --output-ranked-attestations to also write to file.',
        )

    def handle(self, *args, **options):
        # Check if importing JSON
        import_json = options.get('import_json')
        if import_json:
            return self._import_json_trial(import_json, options)
        
        # Determine week end date
        # If --week is provided, use it; otherwise use --week-end
        week_str = options.get('week')
        week_end_str = options.get('week_end')
        
        if week_str and week_end_str:
            raise CommandError('Cannot specify both --week and --week-end. Use only one.')
        
        if week_str:
            # Calculate week start (Monday) from publication date
            # If publication date is Monday, use the previous week (that just ended)
            week_start = self._get_week_start_from_publication_date(week_str)
            week_end = week_start + timedelta(days=6)
            # Validate it's actually a Monday
            if week_start.weekday() != 0:
                raise CommandError(f'Calculated week start is not a Monday. This should not happen.')
            # Validate it's actually a Sunday
            if week_end.weekday() != 6:
                raise CommandError(f'Calculated week end is not a Sunday. This should not happen.')
            # Show helpful message
            given_date = date.fromisoformat(week_str)
            if given_date.weekday() == 0:  # Monday
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Publication date {week_str} (Monday) → Using previous week: {week_start} to {week_end}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Publication date {week_str} ({given_date.strftime('%A')}) → Using previous week: {week_start} to {week_end}"
                    )
                )
        else:
            week_end = self._get_week_end(week_end_str)
            week_start = week_end - timedelta(days=6)
        
        self.stdout.write(f"Week: {week_start} to {week_end}")
        
        # Get weekly roll call
        try:
            roll_call = WeeklyRollCall.objects.get(week_start_date=week_start)
        except WeeklyRollCall.DoesNotExist:
            raise CommandError(f'No weekly roll call found for week starting {week_start}')
        
        # Check if output-only mode
        if options.get('output_only'):
            # Get existing trials
            all_trials = list(RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by('trial_number'))
            if not all_trials:
                raise CommandError(f'No trials found for week starting {week_start}')
            
            # Calculate stats
            stats = calculate_cumulative_stats(all_trials)
            
            # Display cumulative rankings
            self.stdout.write(f"\nFound {len(all_trials)} existing trial(s) for week {week_start}")
            self.stdout.write(format_rankings_display(stats))
            
            # Check convergence
            convergence = check_convergence(all_trials, threshold=0.5)
            
            self.stdout.write("\n" + "="*80)
            self.stdout.write("CONVERGENCE STATUS")
            self.stdout.write("="*80)
            self.stdout.write(f"Converged: {convergence['converged']}")
            self.stdout.write(f"Reason: {convergence['reason']}")
            self.stdout.write(f"Trials: {convergence['current_trials']}")
            if convergence['max_std_error'] is not None:
                self.stdout.write(f"Max Std Error: {convergence['max_std_error']:.3f}")
            self.stdout.write("="*80)
            
            # If output file is specified, also write ranked attestations
            output_file = options.get('output_ranked_attestations')
            if output_file:
                # Get attestations (exclude hidden ones)
                attestations_queryset = Attestation.objects.filter(
                    weekly_roll_call=roll_call,
                    is_hidden=False
                ).order_by('posted_at', 'part_number')
                
                if not attestations_queryset.exists():
                    self.stdout.write(self.style.WARNING(f'No attestations found for week starting {week_start} - skipping file output'))
                else:
                    attestations_list = list(attestations_queryset)
                    self.stdout.write(f"\nOutputting ranked attestations to: {output_file}")
                    self._write_ranked_attestations(roll_call, stats, attestations_list, output_file)
            
            return
        
        # Fetch all attestations for that week (exclude hidden ones)
        attestations_queryset = Attestation.objects.filter(
            weekly_roll_call=roll_call,
            is_hidden=False
        ).order_by('posted_at', 'part_number')
        
        if not attestations_queryset.exists():
            raise CommandError(f'No attestations found for week starting {week_start}')
        
        # Convert to list and randomize order to avoid order bias
        attestations_list = list(attestations_queryset)
        random.shuffle(attestations_list)
        
        self.stdout.write(f"Found {len(attestations_list)} attestation(s)")
        self.stdout.write(self.style.WARNING("⚠️  Attestations have been randomized to avoid order bias"))
        
        # Show participant list (randomized order)
        self._show_participant_list(attestations_list)
        
        # Build mapping from clean names (what AI sees) to canonical names (what we store)
        # Check existing trials to see what names were used before
        existing_trials = RankingTrial.objects.filter(weekly_roll_call=roll_call)
        existing_names = set()
        for trial in existing_trials:
            if trial.parsed_rankings:
                for item in trial.parsed_rankings:
                    existing_names.add(item.get('name', '').strip())
        
        # Enhanced normalization that handles spaces/underscores (same as fix command)
        def enhanced_normalize(name):
            """Normalize name with additional handling for spaces/underscores"""
            norm = normalize_name(name)
            # Also normalize spaces/underscores for better matching
            norm = norm.replace('_', ' ').strip()
            # Collapse multiple spaces
            while '  ' in norm:
                norm = norm.replace('  ', ' ')
            return norm.lower()
        
        # Build mapping: prefer existing names if they match, otherwise use clean name
        # Also build reverse mapping from normalized names to canonical names for lookup
        clean_name_to_canonical = {}
        normalized_to_canonical = {}  # normalized -> canonical (most common variant)
        
        # First, build normalized -> canonical mapping from existing names
        from collections import Counter
        normalized_counts = {}
        for existing_name in existing_names:
            norm = enhanced_normalize(existing_name)
            if norm not in normalized_counts:
                normalized_counts[norm] = []
            normalized_counts[norm].append(existing_name)
        
        # For each normalized name, pick the most common variant as canonical
        for norm, variants in normalized_counts.items():
            variant_counts = Counter(variants)
            canonical = variant_counts.most_common(1)[0][0]
            normalized_to_canonical[norm] = canonical
        
        # Now build clean_name -> canonical mapping
        for attestation in attestations_list:
            clean_name = self._get_clean_name(attestation)
            if clean_name and clean_name != "Unknown User":
                # Normalize and look up canonical name
                clean_normalized = enhanced_normalize(clean_name)
                canonical_name = normalized_to_canonical.get(clean_normalized, clean_name)
                clean_name_to_canonical[clean_name] = canonical_name
        
        # Format attestations into text (already randomized)
        attestations_text = self._format_attestations(attestations_list, options.get('thinking', False))
        
        # Check available providers
        available_providers = get_available_providers()
        excluded = set(options.get('exclude_provider') or [])
        if excluded:
            available_providers = [p for p in available_providers if p not in excluded]
        if not available_providers:
            raise CommandError('No AI provider API keys configured. Set OPENAI_API_KEY, ANTHROPIC_API_KEY, GROK_API_KEY, or DEEPSEEK_API_KEY.')
        
        self.stdout.write(f"Available providers: {', '.join(available_providers)}")
        
        # Show thinking mode status
        if options.get('thinking', False):
            self.stdout.write(self.style.SUCCESS("Thinking mode: ENABLED"))
        
        # Show file attachment mode status
        if options.get('use_file_attachment', False):
            self.stdout.write(self.style.SUCCESS("File attachment mode: ENABLED (attestations will be in separate content blocks for better structure)"))
            self.stdout.write(self.style.WARNING("  Note: For text content, this is mainly organizational - token count is the same as inline"))
        
        # Dry run mode - just show what would be sent
        if options.get('dry_run', False):
            self._show_dry_run(options, available_providers, attestations_text, attestations_list, roll_call)
            return
        
        # Run trials
        auto_continue = options.get('auto_continue', False)
        
        while True:
            # Select provider
            if options.get('provider'):
                provider = options['provider']
                if provider not in available_providers:
                    raise CommandError(f'Provider {provider} not available. Available: {", ".join(available_providers)}')
            else:
                if not available_providers:
                    raise CommandError('No available providers after exclusions')
                provider = random.choice(available_providers)
            
            self.stdout.write(f"\n{'='*80}")
            self.stdout.write(f"Running trial with {provider}...")
            
            # Make API call
            thinking_mode = options.get('thinking', False)
            use_file_attachment = options.get('use_file_attachment', False)

            if use_file_attachment:
                self.stdout.write(self.style.SUCCESS("Using file attachment mode for attestations"))

            # Save prompt to file (overwrites each time - just need latest)
            import os
            base_logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'logs')
            week_logs_dir = os.path.join(base_logs_dir, str(week_end))
            os.makedirs(week_logs_dir, exist_ok=True)

            final_prompt = RANKING_PROMPT
            if thinking_mode:
                from rollcall.services.ai_ranking import THINKING_PROMPT_ADDITION
                final_prompt = RANKING_PROMPT + THINKING_PROMPT_ADDITION
            full_prompt = f"{final_prompt}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"

            prompt_file = os.path.join(week_logs_dir, "prompt.txt")
            with open(prompt_file, 'w', encoding='utf-8') as f:
                f.write(full_prompt)

            result = None
            if provider == 'openai':
                result = call_openai(attestations_text, RANKING_PROMPT, thinking_mode, use_file_attachment)
            elif provider == 'anthropic':
                result = call_anthropic(attestations_text, RANKING_PROMPT, thinking_mode, use_file_attachment)
            elif provider == 'grok':
                result = call_grok(attestations_text, RANKING_PROMPT, thinking_mode)
            elif provider == 'deepseek':
                result = call_deepseek(attestations_text, RANKING_PROMPT, thinking_mode, use_file_attachment)
            
            if not result:
                self.stdout.write(self.style.ERROR(f'Failed to get response from {provider}'))
                if not auto_continue:
                    return
                continue
            
            model_name, raw_response, cost_info = result
            
            # Display cost estimate
            self.stdout.write("\n" + "-"*80)
            self.stdout.write("COST ESTIMATE:")
            self.stdout.write("-"*80)
            self.stdout.write(f"Input tokens: {cost_info['input_tokens']:,}")
            self.stdout.write(f"Output tokens: {cost_info['output_tokens']:,}")
            self.stdout.write(f"Input cost: ${cost_info['input_cost']:.4f}")
            self.stdout.write(f"Output cost: ${cost_info['output_cost']:.4f}")
            self.stdout.write(self.style.SUCCESS(f"Total cost: ${cost_info['total_cost']:.4f}"))
            self.stdout.write("-"*80)
            
            # Parse JSON response
            parsed_rankings = parse_ranking_response(raw_response)
            
            if not parsed_rankings:
                self.stdout.write(self.style.ERROR('Failed to parse ranking response'))
                self.stdout.write(f"Raw response (first 500 chars): {raw_response[:500]}")
                if not auto_continue:
                    return
                continue
            
            # Map AI response names back to canonical names using enhanced normalized matching
            # The AI may return clean names, but we need to ensure consistency with existing trials
            canonical_rankings = []
            for ranking in parsed_rankings:
                ai_name = ranking.get('name', '').strip()
                rank = ranking.get('rank', 0)
                
                # Use enhanced normalization (handles spaces/underscores)
                ai_normalized = enhanced_normalize(ai_name)
                
                # Try to find canonical name using normalized lookup
                canonical_name = None
                
                # First, try direct lookup in normalized_to_canonical (from existing trials)
                if ai_normalized in normalized_to_canonical:
                    canonical_name = normalized_to_canonical[ai_normalized]
                
                # If not found, try matching against clean names in our mapping
                if not canonical_name:
                    for clean_name, canonical in clean_name_to_canonical.items():
                        if enhanced_normalize(clean_name) == ai_normalized:
                            canonical_name = canonical
                            break
                
                # If still not found, use the AI's name as-is (might be a new participant)
                # But add it to the mapping for future reference
                if not canonical_name:
                    canonical_name = ai_name
                    # Add to normalized mapping for future lookups
                    normalized_to_canonical[ai_normalized] = ai_name
                    clean_name_to_canonical[ai_name] = ai_name
                
                canonical_rankings.append({
                    'rank': rank,
                    'name': canonical_name
                })
            
            parsed_rankings = canonical_rankings
            
            # Get next trial number (recalculate each time to avoid race conditions)
            # Use max() to get the highest trial number, or 0 if no trials exist
            max_trial = RankingTrial.objects.filter(weekly_roll_call=roll_call).aggregate(
                max_trial=models.Max('trial_number')
            )['max_trial'] or 0
            trial_number = max_trial + 1
            
            # Create RankingTrial record
            trial = RankingTrial.objects.create(
                weekly_roll_call=roll_call,
                trial_number=trial_number,
                ai_provider=provider,
                ai_model=model_name,
                raw_response=raw_response,
                parsed_rankings=parsed_rankings
            )
            
            self.stdout.write(self.style.SUCCESS(f'✅ Trial {trial_number} completed'))
            self.stdout.write(f"Model: {model_name}")
            self.stdout.write(f"Rankings: {len(parsed_rankings)} participants")
            
            # Display current trial results
            self.stdout.write("\n" + "="*80)
            self.stdout.write("CURRENT TRIAL RANKINGS")
            self.stdout.write("="*80)
            for item in parsed_rankings:
                rank = item.get('rank', 0)
                name = item.get('name', '')
                self.stdout.write(f"{rank:3d}. {name}")
            
            # Get all trials for this week
            all_trials = list(RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by('trial_number'))
            
            # Calculate cumulative statistics
            stats = calculate_cumulative_stats(all_trials)
            
            # Display cumulative rankings
            self.stdout.write(format_rankings_display(stats))
            
            # Check convergence
            convergence = check_convergence(all_trials, threshold=0.5)
            
            self.stdout.write("\n" + "="*80)
            self.stdout.write("CONVERGENCE STATUS")
            self.stdout.write("="*80)
            self.stdout.write(f"Converged: {convergence['converged']}")
            self.stdout.write(f"Reason: {convergence['reason']}")
            self.stdout.write(f"Trials: {convergence['current_trials']}")
            if convergence['max_std_error'] is not None:
                self.stdout.write(f"Max Std Error: {convergence['max_std_error']:.3f}")
            self.stdout.write("="*80)
            
            # Save raw response to file for debugging (optional)
            import os
            base_logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'logs')
            # Organize by week end date (publication date - 1 day = Sunday)
            week_logs_dir = os.path.join(base_logs_dir, str(week_end))
            os.makedirs(week_logs_dir, exist_ok=True)
            debug_file = os.path.join(week_logs_dir, f"trial_{trial_number}_{provider}_{model_name.replace('-', '_')}.json")
            try:
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        'trial_number': trial_number,
                        'provider': provider,
                        'model': model_name,
                        'raw_response': raw_response,
                        'parsed_rankings': parsed_rankings,
                        'created_at': trial.created_at.isoformat()
                    }, f, indent=2)
                self.stdout.write(f"\nDebug file saved: {debug_file}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Could not save debug file: {e}"))
            
            # Output ranked attestations if requested (at end of each trial or when converged)
            output_file = options.get('output_ranked_attestations')
            if output_file:
                self._write_ranked_attestations(roll_call, stats, attestations_list, output_file)
            
            # Check if we should continue
            if convergence['converged']:
                self.stdout.write(self.style.SUCCESS("\n✅ Rankings have converged!"))
                break
            
            if not auto_continue:
                # Ask user if they want to run another trial
                self.stdout.write("\n")
                response = input("Run another trial? (y/n): ").strip().lower()
                if response != 'y':
                    break
            else:
                self.stdout.write("\nContinuing to next trial...\n")
    
    def _show_dry_run(self, options, available_providers, attestations_text, attestations_list, roll_call):
        """
        Show what would be sent to AI providers without making API calls.
        """
        self.stdout.write("\n" + "="*80)
        self.stdout.write(self.style.WARNING("DRY RUN MODE - No API calls will be made"))
        self.stdout.write(self.style.WARNING("⚠️  Note: Attestations and participants are randomized to avoid order bias"))
        self.stdout.write("="*80 + "\n")
        
        # Show which attestations will be included
        self.stdout.write("\n" + "="*80)
        self.stdout.write(f"ATTESTATIONS TO BE RANKED ({len(attestations_list)} total):")
        self.stdout.write("="*80)
        self.stdout.write(f"Week: {roll_call.week_start_date} to {roll_call.week_end_date}")
        self.stdout.write("")
        
        # Group by participant and show summary
        from collections import defaultdict
        participant_attestations = defaultdict(list)
        for att in attestations_list:
            name = self._get_clean_name(att)
            if name and name != "Unknown User":
                participant_attestations[name].append(att)
        
        # Show participants with their attestation counts and dates
        for i, (name, atts) in enumerate(sorted(participant_attestations.items()), 1):
            posted_dates = sorted([att.posted_at.date() for att in atts])
            date_range = f"{posted_dates[0]} to {posted_dates[-1]}" if len(posted_dates) > 1 else str(posted_dates[0])
            parts = len(atts)
            part_text = f"{parts} part{'s' if parts > 1 else ''}"
            has_attachments = any(att.has_attachments for att in atts)
            attachment_indicator = " 📎" if has_attachments else ""
            self.stdout.write(f"  {i:2d}. {name} ({part_text}, posted: {date_range}){attachment_indicator}")
        
        self.stdout.write("="*80 + "\n")
        
        # Determine which provider would be used
        if options.get('provider'):
            provider = options['provider']
            if provider not in available_providers:
                self.stdout.write(self.style.ERROR(f'Provider {provider} not available. Available: {", ".join(available_providers)}'))
                return
        else:
            provider = select_random_provider()
            if not provider:
                self.stdout.write(self.style.ERROR('No available providers'))
                return
        
        self.stdout.write(f"Selected provider: {self.style.SUCCESS(provider)}")
        self.stdout.write(f"Available providers: {', '.join(available_providers)}\n")
        
        # Show the full prompt that would be sent
        thinking_mode = options.get('thinking', False)
        final_prompt = RANKING_PROMPT
        if thinking_mode:
            from rollcall.services.ai_ranking import THINKING_PROMPT_ADDITION
            final_prompt = RANKING_PROMPT + THINKING_PROMPT_ADDITION
        
        full_prompt = f"{final_prompt}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"

        # Save prompt to file (overwrites each time)
        import os
        base_logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'logs')
        week_logs_dir = os.path.join(base_logs_dir, str(roll_call.week_end_date))
        os.makedirs(week_logs_dir, exist_ok=True)
        prompt_file = os.path.join(week_logs_dir, "prompt.txt")
        with open(prompt_file, 'w', encoding='utf-8') as f:
            f.write(full_prompt)
        self.stdout.write(f"Prompt saved to: {prompt_file}\n")

        # Show cost estimate and token limits
        from rollcall.services.ai_ranking import estimate_tokens, estimate_cost_openai, estimate_cost_anthropic, estimate_cost_deepseek
        
        input_tokens_est = estimate_tokens(full_prompt)
        # Estimate output tokens: more for thinking mode
        output_tokens_est = 2000 if thinking_mode else 500
        
        # Check context window limits
        if provider == 'openai':
            context_limit = 8192  # GPT-4 standard
            if input_tokens_est > context_limit * 0.9:
                self.stdout.write(self.style.WARNING(
                    f"\n⚠️  WARNING: Input tokens ({input_tokens_est:,}) approaching GPT-4 context limit ({context_limit:,}). "
                    f"Consider using gpt-4-turbo (128K context) instead."
                ))
            cost_info = estimate_cost_openai(input_tokens_est, output_tokens_est)
        elif provider == 'anthropic':
            context_limit = 200000  # Claude 3 Opus
            if input_tokens_est > context_limit * 0.9:
                self.stdout.write(self.style.WARNING(
                    f"\n⚠️  WARNING: Input tokens ({input_tokens_est:,}) approaching Claude 3 Opus context limit ({context_limit:,})."
                ))
            else:
                # Show that we're well within limits
                usage_pct = (input_tokens_est / context_limit) * 100
                self.stdout.write(f"\n✓ Input size: {input_tokens_est:,} tokens ({usage_pct:.1f}% of {context_limit:,} context window)")
            cost_info = estimate_cost_anthropic(input_tokens_est, output_tokens_est)
        elif provider == 'deepseek':
            context_limit = 32000  # DeepSeek Chat
            if input_tokens_est > context_limit * 0.9:
                self.stdout.write(self.style.WARNING(
                    f"\n⚠️  WARNING: Input tokens ({input_tokens_est:,}) approaching DeepSeek context limit ({context_limit:,})."
                ))
            else:
                usage_pct = (input_tokens_est / context_limit) * 100
                self.stdout.write(f"\n✓ Input size: {input_tokens_est:,} tokens ({usage_pct:.1f}% of {context_limit:,} context window)")
            cost_info = estimate_cost_deepseek(input_tokens_est, output_tokens_est)
        else:
            cost_info = None
        
        if cost_info:
            self.stdout.write("\n" + "-"*80)
            self.stdout.write("ESTIMATED COST:")
            self.stdout.write("-"*80)
            self.stdout.write(f"Input tokens (est): {cost_info['input_tokens']:,}")
            self.stdout.write(f"Output tokens (est): {cost_info['output_tokens']:,}")
            if thinking_mode:
                self.stdout.write(self.style.WARNING("  (Higher output estimate due to thinking mode)"))
            self.stdout.write(f"Estimated total cost: ${cost_info['total_cost']:.4f}")
            self.stdout.write("-"*80 + "\n")
        
        self.stdout.write("="*80)
        self.stdout.write("FULL PROMPT THAT WOULD BE SENT:")
        self.stdout.write("="*80)
        self.stdout.write(full_prompt)
        self.stdout.write("="*80 + "\n")
        
        # Show provider-specific details
        self.stdout.write("="*80)
        self.stdout.write(f"PROVIDER-SPECIFIC DETAILS ({provider.upper()}):")
        self.stdout.write("="*80)
        
        if provider == 'openai':
            # Estimate tokens to determine which model will be used
            from rollcall.services.ai_ranking import estimate_tokens
            full_prompt_est = f"{RANKING_PROMPT}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"
            if thinking_mode:
                from rollcall.services.ai_ranking import THINKING_PROMPT_ADDITION
                full_prompt_est = f"{RANKING_PROMPT}{THINKING_PROMPT_ADDITION}\n\nAttestations:\n{attestations_text}\n\nOutput the JSON ranking list:"
            input_tokens_est = estimate_tokens(full_prompt_est)
            
            # Determine which model will be used
            if input_tokens_est > 6000 or thinking_mode:
                model_name = "gpt-4-turbo-preview"
                max_tokens = 4000 if thinking_mode else 2000
                context_window = "128,000 tokens"
            else:
                model_name = "gpt-4"
                available_for_output = 8192 - input_tokens_est - 100
                max_tokens = min(4000 if thinking_mode else 2000, max(100, available_for_output))
                if max_tokens < 500:
                    model_name = "gpt-4-turbo-preview (auto-switched due to size)"
                    max_tokens = 4000 if thinking_mode else 2000
                    context_window = "128,000 tokens"
                else:
                    context_window = "8,192 tokens"
            
            self.stdout.write(f"Model: {model_name}")
            self.stdout.write("System message: 'You are an expert at evaluating warrior fitness and battle-readiness.'")
            self.stdout.write("User message: [Full prompt shown above]")
            self.stdout.write("Temperature: 0.7")
            self.stdout.write(f"Max output tokens: {max_tokens}")
            self.stdout.write(f"Context window: {context_window}")
            self.stdout.write(f"Estimated input tokens: ~{input_tokens_est:,}")
        elif provider == 'anthropic':
            max_tokens = 4096  # Maximum allowed for claude-3-opus-20240229
            use_file_attachment = options.get('use_file_attachment', False)
            self.stdout.write("Model: claude-3-opus-20240229")
            if use_file_attachment:
                self.stdout.write("User message: [Prompt + attestations attached as text file]")
            else:
                self.stdout.write("User message: [Full prompt shown above]")
            self.stdout.write(f"Max output tokens: {max_tokens} (maximum for this model)")
            self.stdout.write("Context window: 200,000 tokens")
            if use_file_attachment:
                self.stdout.write("File attachment: Yes (attestations as text file)")
        elif provider == 'grok':
            self.stdout.write("Model: [Not yet implemented]")
        elif provider == 'deepseek':
            max_tokens = 4000 if thinking_mode else 2000
            use_file_attachment = options.get('use_file_attachment', False)
            self.stdout.write("Model: deepseek-chat")
            if use_file_attachment:
                self.stdout.write("User message: [Prompt + attestations attached as text file]")
            else:
                self.stdout.write("User message: [Full prompt shown above]")
            self.stdout.write(f"Max output tokens: {max_tokens}")
            self.stdout.write("Context window: 32,000 tokens")
            self.stdout.write("Base URL: https://api.deepseek.com/v1")
            if use_file_attachment:
                self.stdout.write("File attachment: Yes (attestations as text file)")
        
        self.stdout.write("="*80 + "\n")
        
        # Show attestations summary
        # Use the actual participant count from the grouped attestations
        # (we already calculated this earlier in the function)
        participant_count = len(participant_attestations)
        
        self.stdout.write(f"\nAttestations summary:")
        self.stdout.write(f"  - Number of participants: {participant_count}")
        self.stdout.write(f"  - Total attestation parts: {len(attestations_list)}")
        self.stdout.write(f"  - Total characters: {len(attestations_text):,}")
        self.stdout.write(f"  - Total words: {len(attestations_text.split()):,}")
        
        self.stdout.write("\n" + "="*80)
        self.stdout.write(self.style.SUCCESS("Dry run complete. Use without --dry-run to execute."))
        self.stdout.write("="*80 + "\n")
    
    def _get_clean_name(self, attestation):
        """
        Get clean name for an attestation, preferring linked_name (Substack format)
        over raw Discord/Telegram usernames.
        
        Args:
            attestation: Attestation object
        
        Returns:
            Clean name string
        """
        user_mapping = attestation.user_mapping
        if user_mapping:
            # Prefer linked_name (Substack format) if available
            if hasattr(user_mapping, 'linked_name') and user_mapping.linked_name:
                return user_mapping.linked_name
            
            # Fallback to Discord/Telegram names
            if attestation.source == 'discord':
                return user_mapping.discord_username or user_mapping.discord_display_name or "Unknown User"
            else:
                telegram_name = f"{user_mapping.telegram_first_name} {user_mapping.telegram_last_name}".strip()
                return telegram_name or user_mapping.telegram_username or "Unknown User"
        else:
            return "Unknown User"
    
    def _get_week_start_from_publication_date(self, date_str: str) -> date:
        """
        Get week start date (Monday) from publication date.
        
        If publication date is a Monday, returns the Monday of the previous week
        (the week that just ended). Otherwise, calculates the Monday of the week
        containing the date, then returns the previous week's Monday.
        
        Args:
            date_str: Publication date (typically a Monday) in YYYY-MM-DD format
        
        Returns:
            Monday date of the week that just ended
        """
        try:
            given_date = date.fromisoformat(date_str)
            weekday = given_date.weekday()
            
            # If it's Monday (weekday 0), the week that just ended is 7 days back
            if weekday == 0:
                # Previous week's Monday (7 days back)
                return given_date - timedelta(days=7)
            else:
                # Calculate the Monday of the week containing this date
                days_back_to_monday = weekday
                this_week_monday = given_date - timedelta(days=days_back_to_monday)
                # Then go back 7 days to get the previous week's Monday
                return this_week_monday - timedelta(days=7)
        except ValueError:
            raise CommandError(f'Invalid date format: {date_str}. Use YYYY-MM-DD format.')
    
    def _get_week_end(self, week_end_str: str = None) -> date:
        """
        Get week end date (Sunday).
        
        If not provided, defaults to most recent Sunday.
        """
        if week_end_str:
            try:
                week_end = date.fromisoformat(week_end_str)
                if week_end.weekday() != 6:  # Sunday is 6
                    raise CommandError(
                        f'Week end date must be a Sunday. {week_end} is a {week_end.strftime("%A")}. '
                        f'Tip: Use --week-start {week_end} to automatically calculate the Sunday of that week.'
                    )
                return week_end
            except ValueError:
                raise CommandError(f'Invalid date format: {week_end_str}. Use YYYY-MM-DD format.')
        else:
            # Default to most recent Sunday
            today = date.today()
            days_since_sunday = (today.weekday() + 1) % 7
            if days_since_sunday == 0:
                # Today is Sunday
                return today
            else:
                # Go back to last Sunday
                return today - timedelta(days=days_since_sunday)
    
    def _show_participant_list(self, attestations):
        """
        Show list of participants who will be ranked.
        """
        # Get unique participants using clean names
        participants = set()
        for attestation in attestations:
            name = self._get_clean_name(attestation)
            if name and name != "Unknown User":
                participants.add(name)
        
        # Randomize participant list order (not sorted) to avoid order bias
        participant_list = list(participants)
        random.shuffle(participant_list)
        
        self.stdout.write("\n" + "="*80)
        self.stdout.write(f"PARTICIPANTS TO BE RANKED ({len(participant_list)} total, randomized order):")
        self.stdout.write("="*80)
        
        # Create a mapping of names to attachment status
        name_to_attachments = {}
        for attestation in attestations:
            name = self._get_clean_name(attestation)
            if name and name != "Unknown User":
                if name not in name_to_attachments:
                    name_to_attachments[name] = False
                if attestation.has_attachments:
                    name_to_attachments[name] = True
        
        # Show first 10, or all if less than 10
        display_count = min(10, len(participant_list))
        for i, name in enumerate(participant_list[:display_count], 1):
            has_attachments = name_to_attachments.get(name, False)
            attachment_indicator = " 📎" if has_attachments else ""
            self.stdout.write(f"  {i:2d}. {name}{attachment_indicator}")
        
        if len(participant_list) > 10:
            self.stdout.write(f"  ... and {len(participant_list) - 10} more")
        
        self.stdout.write("="*80 + "\n")
    
    def _format_attestations(self, attestations, thinking_mode: bool = False) -> str:
        """
        Format attestations into text for AI prompt.
        
        Groups multi-part attestations together.
        Adds image notes for participants with attachments.
        """
        lines = []
        
        # Group by parent attestation or individual
        attestation_groups = {}
        for attestation in attestations:
            if attestation.parent_attestation:
                parent_id = attestation.parent_attestation.id
                if parent_id not in attestation_groups:
                    attestation_groups[parent_id] = []
                attestation_groups[parent_id].append(attestation)
            else:
                # Standalone attestation
                attestation_groups[attestation.id] = [attestation]
        
        # Format each group (randomize order to avoid bias)
        group_items = list(attestation_groups.items())
        random.shuffle(group_items)
        
        for group_id, group_attestations in group_items:
            # Sort by part number
            sorted_parts = sorted(group_attestations, key=lambda a: a.part_number)
            
            # Get clean name (preferring linked_name/Substack format)
            first_attestation = sorted_parts[0]
            name = self._get_clean_name(first_attestation)
            
            # Combine all parts
            parts_text = []
            for part in sorted_parts:
                parts_text.append(part.raw_text)
            
            full_text = "\n\n".join(parts_text)
            
            # Check for attachments and add image note
            has_attachments = any(part.has_attachments for part in sorted_parts)
            attachment_count = sum(part.attachment_count for part in sorted_parts)
            
            # Format as: "Name: [attestation text]"
            lines.append(f"{name}:")
            
            # Add image note if attachments exist
            if has_attachments:
                # Special handling for DefiShaka's image
                if name.lower() in ['defishaka', 'defi shaka', 'defishaka']:
                    lines.append("")
                    lines.append("=" * 80)
                    lines.append(f"IMAGE ATTACHMENT FOR {name.upper()}:")
                    lines.append("=" * 80)
                    lines.append("This participant (DefiShaka) has included an image attachment: 12-1-DefiShaka-Image.jpg")
                    lines.append("")
                    lines.append("IMAGE CONTENT DESCRIPTION:")
                    lines.append("The image shows a performance graph titled '0.5 Mile Dumbbell Farmer Carry (30s rest between carries)'")
                    lines.append("comparing two progressions:")
                    lines.append("")
                    lines.append("PROGRESSION 1 (Baseline):")
                    lines.append("  - 35 lbs per hand: 13 minutes 50.0 seconds")
                    lines.append("  - 40 lbs per hand: 15 minutes 06.0 seconds")
                    lines.append("  - 45 lbs per hand: 16 minutes 43.0 seconds")
                    lines.append("  - 50 lbs per hand: 20 minutes 28.0 seconds")
                    lines.append("")
                    lines.append("PROGRESSION 2 (Improved):")
                    lines.append("  - 35 lbs per hand: 11 minutes 53.0 seconds (improvement: -1:57)")
                    lines.append("  - 40 lbs per hand: 12 minutes 22.0 seconds (improvement: -2:44)")
                    lines.append("")
                    lines.append("This demonstrates significant performance improvement - faster completion times at the same")
                    lines.append("weights, indicating increased strength-endurance and battle-readiness. This visual evidence")
                    lines.append("should be weighted heavily when ranking, as it shows concrete, measurable progress.")
                    lines.append("=" * 80)
                    lines.append("")
                else:
                    lines.append("")
                    lines.append(f"[NOTE: {name} has {attachment_count} attachment(s) including images or other media.")
                    lines.append("Consider any visual evidence provided when evaluating their attestation.]")
                    lines.append("")
            
            lines.append(f"{full_text}")
            lines.append("")  # Blank line between attestations
        
        return "\n".join(lines)
    
    def _write_ranked_attestations(self, roll_call, stats, attestations_list, output_file):
        """
        Write attestations in final ranked order to a file.
        
        Args:
            roll_call: WeeklyRollCall object
            stats: Cumulative statistics dictionary from calculate_cumulative_stats
            attestations_list: List of Attestation objects
            output_file: Path to output file
        """
        import os
        
        # Sort stats by average rank to get final order
        sorted_stats = sorted(stats.items(), key=lambda x: x[1]['average_rank'])
        
        # Build mapping from clean names to attestations
        name_to_attestations = {}
        for attestation in attestations_list:
            clean_name = self._get_clean_name(attestation)
            if clean_name and clean_name != "Unknown User":
                if clean_name not in name_to_attestations:
                    name_to_attestations[clean_name] = []
                name_to_attestations[clean_name].append(attestation)
        
        # Enhanced normalization for matching
        def enhanced_normalize(name):
            """Normalize name with additional handling for spaces/underscores"""
            from rollcall.services.ranking_stats import normalize_name
            norm = normalize_name(name)
            norm = norm.replace('_', ' ').strip()
            while '  ' in norm:
                norm = norm.replace('  ', ' ')
            return norm.lower()
        
        # Write to file
        lines = []
        lines.append("=" * 80)
        lines.append(f"RANKED ATTESTATIONS - Week of {roll_call.week_start_date.strftime('%B %d, %Y')}")
        lines.append("=" * 80)
        lines.append("")
        trial_count = len(RankingTrial.objects.filter(weekly_roll_call=roll_call))
        lines.append(f"Based on {trial_count} trial(s)")
        lines.append("")
        lines.append("=" * 80)
        lines.append("")
        
        for rank, (canonical_name, data) in enumerate(sorted_stats, 1):
            # Get clean display name
            display_name = canonical_name
            if canonical_name.endswith('_manual_import'):
                display_name = canonical_name[:-len('_manual_import')]
            
            # Find attestations for this participant using normalized matching
            participant_attestations = []
            canonical_normalized = enhanced_normalize(canonical_name)
            display_normalized = enhanced_normalize(display_name)
            
            for clean_name, att_list in name_to_attestations.items():
                clean_normalized = enhanced_normalize(clean_name)
                if clean_normalized == canonical_normalized or clean_normalized == display_normalized:
                    participant_attestations.extend(att_list)
            
            # Header
            lines.append("=" * 80)
            lines.append(f"RANK {rank}: {display_name}")
            lines.append("=" * 80)
            lines.append(f"Average Rank: {data['average_rank']:.2f}")
            if data['std_error'] > 0:
                lines.append(f"Std Error: {data['std_error']:.2f}")
            lines.append(f"Trials: {data['trial_count']}")
            lines.append("")
            
            # Attestations
            if participant_attestations:
                # Group multi-part attestations
                attestation_groups = {}
                for att in participant_attestations:
                    if att.parent_attestation:
                        parent_id = att.parent_attestation.id
                        if parent_id not in attestation_groups:
                            attestation_groups[parent_id] = []
                        attestation_groups[parent_id].append(att)
                    else:
                        attestation_groups[att.id] = [att]
                
                # Format each group
                for group_id, group_attestations in attestation_groups.items():
                    sorted_parts = sorted(group_attestations, key=lambda a: a.part_number)
                    parts_text = [part.raw_text for part in sorted_parts]
                    full_text = "\n\n".join(parts_text)
                    
                    lines.append(full_text)
                    lines.append("")
            else:
                lines.append("[No attestations found for this participant]")
                lines.append("")
            
            lines.append("")
        
        # Write to file
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
            self.stdout.write(self.style.SUCCESS(f"\n✅ Ranked attestations written to: {output_file}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n❌ Failed to write ranked attestations: {e}"))
    
    def _import_json_trial(self, json_input: str, options: dict):
        """
        Import a ranking trial from JSON file or JSON string.
        
        Args:
            json_input: Path to JSON file or JSON string
            options: Command options (for week_end or week)
        """
        import os
        
        # Determine week end date (same logic as main handle method)
        week_str = options.get('week')
        week_end_str = options.get('week_end')
        
        if week_str and week_end_str:
            raise CommandError('Cannot specify both --week and --week-end. Use only one.')
        
        if week_str:
            # Calculate week start (Monday) from publication date
            week_start = self._get_week_start_from_publication_date(week_str)
            week_end = week_start + timedelta(days=6)
            # Validate it's actually a Monday
            if week_start.weekday() != 0:
                raise CommandError(f'Calculated week start is not a Monday. This should not happen.')
            # Validate it's actually a Sunday
            if week_end.weekday() != 6:
                raise CommandError(f'Calculated week end is not a Sunday. This should not happen.')
        else:
            week_end = self._get_week_end(week_end_str)
            week_start = week_end - timedelta(days=6)
        
        self.stdout.write(f"Week: {week_start} to {week_end}")
        
        # Get weekly roll call
        try:
            roll_call = WeeklyRollCall.objects.get(week_start_date=week_start)
        except WeeklyRollCall.DoesNotExist:
            raise CommandError(f'No weekly roll call found for week starting {week_start}')
        
        # Try to load JSON - first as file, then as string
        json_data = None
        if os.path.exists(json_input):
            # It's a file path
            self.stdout.write(f"Loading JSON from file: {json_input}")
            try:
                with open(json_input, 'r', encoding='utf-8') as f:
                    json_data = json.load(f)
            except Exception as e:
                raise CommandError(f'Failed to read JSON file: {e}')
        else:
            # Try as JSON string
            self.stdout.write("Parsing JSON string...")
            try:
                json_data = json.loads(json_input)
            except json.JSONDecodeError as e:
                raise CommandError(f'Invalid JSON string: {e}')
        
        # Check if it's a simple array of names (simple format)
        if isinstance(json_data, list) and all(isinstance(item, str) for item in json_data):
            # Simple format: just an array of names in ranking order
            self.stdout.write("Detected simple array format (names in ranking order)")
            
            # Get provider and model from command line args
            provider = options.get('import_provider')
            model_name = options.get('import_model')
            
            if not provider:
                raise CommandError('Simple array format detected. Please provide --import-provider (e.g., --import-provider grok)')
            if not model_name:
                raise CommandError('Simple array format detected. Please provide --import-model (e.g., --import-model grok-2)')
            
            # Convert array to parsed_rankings format
            parsed_rankings = [
                {"rank": i + 1, "name": name.strip()}
                for i, name in enumerate(json_data)
            ]
            raw_response = json.dumps(json_data, indent=2)
            
            self.stdout.write(f"Converted {len(parsed_rankings)} names to rankings (rank 1 = {json_data[0]})")
        
        else:
            # Full format: object with provider, model, etc.
            # Validate required fields
            required_fields = ['provider', 'model']
            missing_fields = [field for field in required_fields if field not in json_data]
            if missing_fields:
                raise CommandError(f'Missing required fields: {", ".join(missing_fields)}. For simple array format, use --import-provider and --import-model')
            
            provider = json_data['provider']
            model_name = json_data['model']
            
            # Get raw_response and parsed_rankings
            raw_response = json_data.get('raw_response', '')
            parsed_rankings = json_data.get('parsed_rankings', [])
            
            # If we have raw_response but no parsed_rankings, try to parse it
            if raw_response and not parsed_rankings:
                self.stdout.write("Parsing rankings from raw_response...")
                parsed_rankings = parse_ranking_response(raw_response)
                if not parsed_rankings:
                    raise CommandError('Could not parse rankings from raw_response. Please provide parsed_rankings in JSON.')
        
        # Validate parsed_rankings
        if not parsed_rankings:
            raise CommandError('No parsed_rankings provided and could not parse from raw_response')
        
        if not isinstance(parsed_rankings, list):
            raise CommandError('parsed_rankings must be a list')
        
        # Validate each ranking entry
        for i, item in enumerate(parsed_rankings):
            if not isinstance(item, dict):
                raise CommandError(f'Ranking item {i} must be a dictionary')
            if 'rank' not in item or 'name' not in item:
                raise CommandError(f'Ranking item {i} must have "rank" and "name" fields')
        
        # Get next trial number
        existing_trials = RankingTrial.objects.filter(weekly_roll_call=roll_call)
        trial_number = existing_trials.count() + 1
        
        # Create RankingTrial record
        trial = RankingTrial.objects.create(
            weekly_roll_call=roll_call,
            trial_number=trial_number,
            ai_provider=provider,
            ai_model=model_name,
            raw_response=raw_response or json.dumps(parsed_rankings, indent=2),
            parsed_rankings=parsed_rankings
        )
        
        self.stdout.write(self.style.SUCCESS(f'✅ Imported trial {trial_number}'))
        self.stdout.write(f"Provider: {provider}")
        self.stdout.write(f"Model: {model_name}")
        self.stdout.write(f"Rankings: {len(parsed_rankings)} participants")
        
        # Display current trial results
        self.stdout.write("\n" + "="*80)
        self.stdout.write("IMPORTED TRIAL RANKINGS")
        self.stdout.write("="*80)
        for item in parsed_rankings:
            rank = item.get('rank', 0)
            name = item.get('name', '')
            self.stdout.write(f"{rank:3d}. {name}")
        
        # Get all trials for this week
        all_trials = list(RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by('trial_number'))
        
        # Calculate cumulative statistics
        stats = calculate_cumulative_stats(all_trials)
        
        # Display cumulative rankings
        self.stdout.write(format_rankings_display(stats))
        
        # Check convergence
        convergence = check_convergence(all_trials, threshold=0.5)
        
        self.stdout.write("\n" + "="*80)
        self.stdout.write("CONVERGENCE STATUS")
        self.stdout.write("="*80)
        self.stdout.write(f"Converged: {convergence['converged']}")
        self.stdout.write(f"Reason: {convergence['reason']}")
        self.stdout.write(f"Trials: {convergence['current_trials']}")
        if convergence['max_std_error'] is not None:
            self.stdout.write(f"Max Std Error: {convergence['max_std_error']:.3f}")
        self.stdout.write("="*80)
