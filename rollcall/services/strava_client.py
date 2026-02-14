"""
Strava API client for OAuth and activity fetching.
"""
import requests
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


class StravaClient:
    """Client for Strava OAuth and API operations"""

    def __init__(self, strava_auth=None):
        """
        Initialize client.

        Args:
            strava_auth: Optional StravaAuth model instance for authenticated requests
        """
        self.strava_auth = strava_auth
        self.client_id = settings.STRAVA_CLIENT_ID
        self.client_secret = settings.STRAVA_CLIENT_SECRET

    @staticmethod
    def get_authorization_url(redirect_uri, state):
        """
        Generate Strava OAuth authorization URL.

        Args:
            redirect_uri: URL to redirect to after authorization
            state: Random state token for CSRF protection

        Returns:
            Full authorization URL
        """
        params = {
            'client_id': settings.STRAVA_CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'scope': 'activity:read_all',
            'state': state,
        }
        query = '&'.join(f"{k}={v}" for k, v in params.items())
        return f"{STRAVA_AUTH_URL}?{query}"

    def exchange_code(self, code):
        """
        Exchange authorization code for access and refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Dict with access_token, refresh_token, expires_at, athlete data

        Raises:
            requests.RequestException on API error
        """
        response = requests.post(
            STRAVA_TOKEN_URL,
            data={
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'code': code,
                'grant_type': 'authorization_code',
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        return {
            'access_token': data['access_token'],
            'refresh_token': data['refresh_token'],
            'expires_at': datetime.fromtimestamp(data['expires_at'], tz=dt_timezone.utc),
            'athlete': data.get('athlete', {}),
        }

    def refresh_token_if_needed(self):
        """
        Refresh the access token if expired.

        Returns:
            True if token was refreshed, False if still valid

        Raises:
            ValueError if no strava_auth configured
            requests.RequestException on API error
        """
        if not self.strava_auth:
            raise ValueError("No strava_auth configured for token refresh")

        if not self.strava_auth.is_token_expired():
            return False

        logger.info(f"Refreshing expired Strava token for athlete {self.strava_auth.athlete_id}")

        response = requests.post(
            STRAVA_TOKEN_URL,
            data={
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'refresh_token': self.strava_auth.refresh_token,
                'grant_type': 'refresh_token',
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        # Update the auth model
        self.strava_auth.access_token = data['access_token']
        self.strava_auth.refresh_token = data['refresh_token']
        self.strava_auth.token_expires_at = datetime.fromtimestamp(
            data['expires_at'], tz=dt_timezone.utc
        )
        self.strava_auth.save()

        logger.info("Strava token refreshed successfully")
        return True

    def _get_headers(self):
        """Get authorization headers for API requests"""
        if not self.strava_auth:
            raise ValueError("No strava_auth configured")
        return {'Authorization': f'Bearer {self.strava_auth.access_token}'}

    def get_athlete(self):
        """
        Get authenticated athlete profile.

        Returns:
            Dict with athlete data
        """
        self.refresh_token_if_needed()

        response = requests.get(
            f"{STRAVA_API_BASE}/athlete",
            headers=self._get_headers(),
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def get_activities(self, after=None, before=None, per_page=50):
        """
        Get athlete activities.

        Args:
            after: Return activities after this datetime (default: 7 days ago)
            before: Return activities before this datetime (default: now)
            per_page: Number of activities per page (max 200)

        Returns:
            List of activity dicts
        """
        self.refresh_token_if_needed()

        if after is None:
            after = timezone.now() - timedelta(days=7)
        if before is None:
            before = timezone.now()

        params = {
            'after': int(after.timestamp()),
            'before': int(before.timestamp()),
            'per_page': min(per_page, 200),
        }

        response = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=self._get_headers(),
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def sync_activities(self, after=None, before=None):
        """
        Fetch and store activities in the database.

        Args:
            after: Fetch activities after this datetime
            before: Fetch activities before this datetime

        Returns:
            Tuple of (created_count, updated_count)
        """
        from rollcall.models import StravaActivity

        activities = self.get_activities(after=after, before=before)
        created = 0
        updated = 0

        for activity_data in activities:
            activity, was_created = StravaActivity.objects.update_or_create(
                activity_id=activity_data['id'],
                defaults={
                    'strava_auth': self.strava_auth,
                    'name': activity_data.get('name', 'Untitled'),
                    'sport_type': activity_data.get('sport_type', activity_data.get('type', 'Unknown')),
                    'start_date': datetime.fromisoformat(
                        activity_data['start_date'].replace('Z', '+00:00')
                    ),
                    'start_date_local': datetime.fromisoformat(
                        activity_data['start_date_local'].replace('Z', '+00:00')
                    ) if activity_data.get('start_date_local') else None,
                    'moving_time_seconds': activity_data.get('moving_time', 0),
                    'elapsed_time_seconds': activity_data.get('elapsed_time', 0),
                    'distance_meters': activity_data.get('distance'),
                    'total_elevation_gain': activity_data.get('total_elevation_gain'),
                    'average_speed': activity_data.get('average_speed'),
                    'max_speed': activity_data.get('max_speed'),
                    'average_heartrate': activity_data.get('average_heartrate'),
                    'max_heartrate': activity_data.get('max_heartrate'),
                    'average_cadence': activity_data.get('average_cadence'),
                    'average_watts': activity_data.get('average_watts'),
                    'suffer_score': activity_data.get('suffer_score'),
                    'kudos_count': activity_data.get('kudos_count', 0),
                    'raw_data': activity_data,
                }
            )
            if was_created:
                created += 1
            else:
                updated += 1

        logger.info(f"Synced Strava activities: {created} created, {updated} updated")
        return created, updated
