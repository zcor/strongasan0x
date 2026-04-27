"""
Ranking Statistics Calculation Module

Provides functions to calculate cumulative statistics, convergence metrics,
and format rankings for display.
"""

import math
from typing import List, Dict, Optional
from collections import defaultdict, Counter
from rollcall.models import RankingTrial


def normalize_name(name: str) -> str:
    """
    Normalize a participant name for matching purposes.
    
    Handles:
    - Case insensitivity
    - Common suffixes like _manual_import
    
    Args:
        name: Raw name from AI response
    
    Returns:
        Normalized name (lowercase, stripped of common suffixes)
    """
    if not name:
        return ""
    
    # Strip and lowercase
    normalized = name.strip().lower()
    
    # Remove common suffixes that don't affect identity
    # Keep the base name for matching
    if normalized.endswith('_manual_import'):
        normalized = normalized[:-len('_manual_import')]
    
    return normalized


def build_canonical_name_map(trials: List[RankingTrial]) -> Dict[str, str]:
    """
    Build a mapping from normalized names to canonical (most common) names.
    
    This handles cases where the AI returns different variations of the same name
    (e.g., "defishaka" vs "Defishaka").
    
    Args:
        trials: List of RankingTrial objects
    
    Returns:
        Dictionary mapping normalized_name -> canonical_name
    """
    # Count occurrences of each name variant
    name_variants = defaultdict(list)  # normalized -> list of original variants
    
    for trial in trials:
        if not trial.parsed_rankings:
            continue
        
        for item in trial.parsed_rankings:
            original_name = item.get('name', '').strip()
            if original_name:
                normalized = normalize_name(original_name)
                name_variants[normalized].append(original_name)
    
    # For each normalized name, pick the most common variant as canonical
    canonical_map = {}
    for normalized, variants in name_variants.items():
        # Count occurrences of each variant
        variant_counts = Counter(variants)
        # Use the most common variant as canonical
        canonical_name = variant_counts.most_common(1)[0][0]
        canonical_map[normalized] = canonical_name

    # Second pass: merge name fragments into their longer forms.
    # e.g. "devin" (1 trial) is a substring of "devin | gammaswap" (13 trials)
    # → map "devin" to the canonical name of "devin | gammaswap"
    normalized_keys = list(canonical_map.keys())
    for short in normalized_keys:
        for long in normalized_keys:
            if short == long:
                continue
            if short in long:
                short_count = len(name_variants[short])
                long_count = len(name_variants[long])
                # Only merge if the short form is clearly a fragment (fewer occurrences)
                if short_count < long_count:
                    canonical_map[short] = canonical_map[long]

    return canonical_map


def calculate_rank_scores(trials: List[RankingTrial]) -> Dict[str, List[int]]:
    """
    Convert ranks to scores for each participant across trials.
    
    Scoring: 1st = 10, 2nd = 9, 3rd = 8, etc.
    Uses normalized names and canonical name mapping.
    
    Args:
        trials: List of RankingTrial objects
    
    Returns:
        Dictionary mapping canonical participant names to lists of scores
    """
    # Build canonical name mapping
    canonical_map = build_canonical_name_map(trials)
    
    # Use normalized names as keys
    scores = defaultdict(list)
    
    for trial in trials:
        if not trial.parsed_rankings:
            continue

        # Track which participants we've already counted for this trial
        seen_in_trial = set()

        # Create a mapping of rank to score (1st = 10, 2nd = 9, etc.)
        max_rank = max(item.get('rank', 0) for item in trial.parsed_rankings)

        for item in trial.parsed_rankings:
            rank = item.get('rank', 0)
            original_name = item.get('name', '').strip()

            if original_name and rank > 0:
                normalized = normalize_name(original_name)

                # Only count first occurrence per participant per trial
                if normalized not in seen_in_trial:
                    seen_in_trial.add(normalized)
                    # Score decreases linearly: 1st = max_rank, 2nd = max_rank-1, etc.
                    # But we want 1st = 10, so we'll use: score = max_rank - rank + 1
                    # Actually, let's use: score = max_rank + 1 - rank (so 1st gets highest)
                    score = max_rank + 1 - rank
                    scores[normalized].append(score)
    
    # Map back to canonical names
    result = {}
    for normalized, score_list in scores.items():
        canonical_name = canonical_map.get(normalized, normalized)
        result[canonical_name] = score_list
    
    return result


def calculate_average_rank(name: str, trials: List[RankingTrial]) -> Optional[float]:
    """
    Calculate average rank for a participant across trials.
    
    Uses normalized name matching to handle case variations and suffixes.
    
    Args:
        name: Participant name (will be normalized for matching)
        trials: List of RankingTrial objects
    
    Returns:
        Average rank or None if participant not found in any trial
    """
    ranks = []
    normalized_search = normalize_name(name)
    
    for trial in trials:
        if not trial.parsed_rankings:
            continue
        
        for item in trial.parsed_rankings:
            item_name = item.get('name', '').strip()
            if normalize_name(item_name) == normalized_search:
                rank = item.get('rank', 0)
                if rank > 0:
                    ranks.append(rank)
                break
    
    if not ranks:
        return None
    
    return sum(ranks) / len(ranks)


def calculate_standard_error(name: str, trials: List[RankingTrial]) -> Optional[float]:
    """
    Calculate standard error of the mean rank for a participant.
    
    Uses normalized name matching to handle case variations and suffixes.
    
    Args:
        name: Participant name (will be normalized for matching)
        trials: List of RankingTrial objects
    
    Returns:
        Standard error or None if participant not found in any trial
    """
    ranks = []
    normalized_search = normalize_name(name)
    
    for trial in trials:
        if not trial.parsed_rankings:
            continue
        
        for item in trial.parsed_rankings:
            item_name = item.get('name', '').strip()
            if normalize_name(item_name) == normalized_search:
                rank = item.get('rank', 0)
                if rank > 0:
                    ranks.append(rank)
                break
    
    if not ranks or len(ranks) < 2:
        return None
    
    mean = sum(ranks) / len(ranks)
    variance = sum((r - mean) ** 2 for r in ranks) / (len(ranks) - 1)
    std_dev = math.sqrt(variance)
    std_error = std_dev / math.sqrt(len(ranks))
    
    return std_error


def calculate_cumulative_stats(trials: List[RankingTrial]) -> Dict[str, Dict]:
    """
    Calculate cumulative statistics for all participants across trials.
    
    Uses normalized names to group variants (e.g., "defishaka" and "Defishaka")
    and returns results with canonical names.
    
    Args:
        trials: List of RankingTrial objects
    
    Returns:
        Dictionary mapping canonical participant names to statistics dictionaries:
        {
            'name': {
                'average_rank': float,
                'std_error': float,
                'trial_count': int,
                'rankings': [int, ...]  # list of ranks across trials
            }
        }
    """
    # Build canonical name mapping
    canonical_map = build_canonical_name_map(trials)
    
    # Use normalized names as keys for grouping
    stats = defaultdict(lambda: {
        'rankings': [],
        'trial_count': 0
    })
    
    # Collect all rankings using normalized names
    # Only count one ranking per participant per trial (take the first/best rank if duplicated)
    for trial in trials:
        if not trial.parsed_rankings:
            continue

        # Track which participants we've already counted for this trial
        seen_in_trial = {}

        for item in trial.parsed_rankings:
            original_name = item.get('name', '').strip()
            rank = item.get('rank', 0)

            if original_name and rank > 0:
                normalized = normalize_name(original_name)

                # Only count first occurrence per participant per trial
                if normalized not in seen_in_trial:
                    seen_in_trial[normalized] = rank
                    stats[normalized]['rankings'].append(rank)
                    stats[normalized]['trial_count'] += 1
    
    # Merge stats for normalized names that map to the same canonical name
    merged_stats = defaultdict(lambda: {'rankings': [], 'trial_count': 0})
    for normalized_name, data in stats.items():
        canonical_name = canonical_map.get(normalized_name, normalized_name)
        merged_stats[canonical_name]['rankings'].extend(data['rankings'])
        merged_stats[canonical_name]['trial_count'] += data['trial_count']

    # Calculate statistics
    result = {}
    for canonical_name, data in merged_stats.items():
        rankings = data['rankings']
        if not rankings:
            continue

        avg_rank = sum(rankings) / len(rankings)

        # Calculate standard error
        if len(rankings) >= 2:
            mean = avg_rank
            variance = sum((r - mean) ** 2 for r in rankings) / (len(rankings) - 1)
            std_dev = math.sqrt(variance)
            std_error = std_dev / math.sqrt(len(rankings))
        else:
            std_error = 0.0

        result[canonical_name] = {
            'average_rank': avg_rank,
            'std_error': std_error,
            'trial_count': len(rankings),
            'rankings': rankings
        }

    return result


def check_convergence(trials: List[RankingTrial], threshold: float = 0.5) -> Dict[str, any]:
    """
    Determine if rankings have converged (stabilized).
    
    Convergence is checked by:
    1. Minimum number of trials (at least 3)
    2. Standard error of average ranks is below threshold
    3. Recent trials show consistent ordering
    
    Args:
        trials: List of RankingTrial objects (should be ordered by trial_number)
        threshold: Maximum standard error for convergence (default 0.5)
    
    Returns:
        Dictionary with convergence status and details:
        {
            'converged': bool,
            'reason': str,
            'min_trials': int,
            'current_trials': int,
            'max_std_error': float
        }
    """
    if len(trials) < 3:
        return {
            'converged': False,
            'reason': f'Need at least 3 trials, have {len(trials)}',
            'min_trials': 3,
            'current_trials': len(trials),
            'max_std_error': None
        }
    
    stats = calculate_cumulative_stats(trials)
    
    if not stats:
        return {
            'converged': False,
            'reason': 'No valid rankings found',
            'min_trials': 3,
            'current_trials': len(trials),
            'max_std_error': None
        }
    
    # Check standard errors
    max_std_error = max(data['std_error'] for data in stats.values())
    
    if max_std_error > threshold:
        return {
            'converged': False,
            'reason': f'Standard error too high: {max_std_error:.2f} > {threshold}',
            'min_trials': 3,
            'current_trials': len(trials),
            'max_std_error': max_std_error
        }
    
    # Check for ties (adjacent rankings with overlapping confidence intervals)
    sorted_stats = sorted(stats.items(), key=lambda x: x[1]['average_rank'])
    ties = []
    for i in range(len(sorted_stats) - 1):
        name1, data1 = sorted_stats[i]
        name2, data2 = sorted_stats[i + 1]
        avg1, se1 = data1['average_rank'], data1['std_error']
        avg2, se2 = data2['average_rank'], data2['std_error']

        # Check if confidence intervals overlap (using 1 SE as the interval)
        # Two rankings are tied if: |avg1 - avg2| < (se1 + se2)
        gap = abs(avg2 - avg1)
        margin = se1 + se2
        if gap < margin:
            ties.append((i + 1, name1, name2, avg1, avg2))

    if ties:
        tie_desc = "; ".join([f"#{t[0]}/{t[0]+1} ({t[1]} vs {t[2]})" for t in ties[:3]])
        return {
            'converged': False,
            'reason': f'Rankings have ties: {tie_desc}',
            'min_trials': 3,
            'current_trials': len(trials),
            'max_std_error': max_std_error,
            'ties': ties
        }

    # Check if recent trials (last 2-3) show consistent top rankings
    recent_trials = sorted(trials, key=lambda t: t.trial_number)[-3:]
    if len(recent_trials) >= 2:
        # Get top 5 from each recent trial, using normalized names
        recent_top_5s = []
        for trial in recent_trials:
            if trial.parsed_rankings:
                top_5 = sorted(trial.parsed_rankings, key=lambda x: x.get('rank', 999))[:5]
                top_5_names = [normalize_name(item.get('name', '').strip()) for item in top_5]
                recent_top_5s.append(set(top_5_names))
        
        # Check overlap between recent trials
        if len(recent_top_5s) >= 2:
            overlap = recent_top_5s[0] & recent_top_5s[-1]
            overlap_ratio = len(overlap) / len(recent_top_5s[0]) if recent_top_5s[0] else 0
            
            if overlap_ratio < 0.6:  # Less than 60% overlap in top 5
                return {
                    'converged': False,
                    'reason': f'Recent trials show inconsistent top rankings (overlap: {overlap_ratio:.1%})',
                    'min_trials': 3,
                    'current_trials': len(trials),
                    'max_std_error': max_std_error
                }
    
    return {
        'converged': True,
        'reason': f'Rankings converged (max std error: {max_std_error:.2f})',
        'min_trials': 3,
        'current_trials': len(trials),
        'max_std_error': max_std_error
    }


def format_rankings_display(stats: Dict[str, Dict]) -> str:
    """
    Format cumulative rankings statistics for console output.
    
    Shows clean names (without _manual_import suffix) for better readability.
    
    Args:
        stats: Dictionary from calculate_cumulative_stats()
    
    Returns:
        Formatted string with rankings and error bars
    """
    if not stats:
        return "No rankings available"
    
    def clean_display_name(name: str) -> str:
        """Convert canonical name to clean display name"""
        # Remove _manual_import suffix
        if name.endswith('_manual_import'):
            name = name[:-len('_manual_import')]
        # Keep original casing (don't lowercase)
        return name
    
    # Sort by average rank
    sorted_stats = sorted(stats.items(), key=lambda x: x[1]['average_rank'])
    
    lines = []
    lines.append("\n" + "=" * 60)
    lines.append("CUMULATIVE RANKINGS")
    lines.append("=" * 60)
    lines.append(f"{'Rank':<6} {'Name':<30} {'Avg Rank':<12} {'Std Error':<12} {'Trials':<8}")
    lines.append("-" * 60)
    
    for rank, (name, data) in enumerate(sorted_stats, 1):
        avg_rank = data['average_rank']
        std_error = data['std_error']
        trial_count = data['trial_count']
        
        # Use clean display name
        display_name = clean_display_name(name)
        
        # Format with error bars: avg ± std_error
        error_str = f"{avg_rank:.2f} ± {std_error:.2f}" if std_error > 0 else f"{avg_rank:.2f}"
        
        lines.append(f"{rank:<6} {display_name:<30} {error_str:<12} {trial_count:<8}")
    
    lines.append("=" * 60)

    return "\n".join(lines)
