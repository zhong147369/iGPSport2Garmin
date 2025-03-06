#!/usr/bin/env python3
"""
Sync iGPSport cycling activities to Garmin Connect.

This script runs periodically in GitHub Actions to download FIT files from iGPSport
and upload them to Garmin Connect, with filtering to avoid duplicates.
"""

import os
import json
import time
import random
import datetime
import requests
import tempfile
import garth
from pathlib import Path
import logging
from dateutil.parser import parse
from typing import Dict, List, Optional, Tuple, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("igpsport-to-garmin")

# Constants
LAST_SYNC_FILE = "last_sync_date.json"
OVERLAP_BUFFER_MINUTES = 5  # Consider activities overlapping if within 5 minutes

class IGPSportClient:
    """Client for the iGPSport API."""
    
    BASE_URL = "https://prod.zh.igpsport.com/service"
    
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.token = None
        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://login.passport.igpsport.cn",
            "referer": "https://login.passport.igpsport.cn/"
        })
    
    def login(self) -> bool:
        """Login to iGPSport."""
        url = f"{self.BASE_URL}/auth/account/login"
        data = {
            "username": self.username,
            "password": self.password,
            "appId": "igpsport-web"
        }
        
        try:
            response = self.session.post(url, json=data)
            response.raise_for_status()
            result = response.json()
            
            if result["code"] == 0 and "data" in result:
                access_token = result["data"]["access_token"]
                self.token = access_token
                self.session.headers.update({
                    "authorization": f"Bearer {access_token}"
                })
                logger.info("Successfully logged in to iGPSport")
                return True
            else:
                logger.error(f"Login failed: {result.get('message', 'Unknown error')}")
                return False
        except Exception as e:
            logger.error(f"Error during login: {e}")
            return False
    
    def get_activities(self, page_no: int = 1, page_size: int = 20) -> Dict:
        """Get list of activities."""
        if not self.token:
            logger.error("Not logged in. Call login() first.")
            return {}
        
        url = f"{self.BASE_URL}/web-gateway/web-analyze/activity/queryMyActivity"
        params = {
            "pageNo": page_no,
            "pageSize": page_size,
            "reqType": 0,
            "sort": 1
        }
        
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            result = response.json()
            
            if result["code"] == 0 and "data" in result:
                return result["data"]
            else:
                logger.error(f"Failed to get activities: {result.get('message', 'Unknown error')}")
                return {}
        except Exception as e:
            logger.error(f"Error getting activities: {e}")
            return {}
    
    def get_activity_detail(self, ride_id: int) -> Dict:
        """Get details for a specific activity."""
        if not self.token:
            logger.error("Not logged in. Call login() first.")
            return {}
        
        url = f"{self.BASE_URL}/web-gateway/web-analyze/activity/queryActivityDetail/{ride_id}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            result = response.json()
            
            if result["code"] == 0 and "data" in result:
                return result["data"]
            else:
                logger.error(f"Failed to get activity detail: {result.get('message', 'Unknown error')}")
                return {}
        except Exception as e:
            logger.error(f"Error getting activity detail: {e}")
            return {}
    
    def download_fit_file(self, fit_url: str) -> Optional[bytes]:
        """Download a FIT file from the given URL."""
        try:
            response = requests.get(fit_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"Error downloading FIT file: {e}")
            return None

class GarminClient:
    """Client for the Garmin Connect API using the garth library."""
    
    def __init__(self, email: str, password: str, domain: str, max_retries: int = 3, retry_delay: int = 5):
        self.email = email
        self.password = password
        self.domain = domain
        self.max_retries = max_retries
        self.retry_delay = retry_delay
    
    def authenticate(self) -> bool:
        """Authenticate with Garmin Connect."""
        try:
            garth.login(self.email, self.password)
            logger.info("Successfully authenticated with Garmin Connect")
            return True
        except Exception as e:
            logger.error(f"Error authenticating with Garmin Connect: {e}")
            return False
    
    def get_activities(self, start_date: Optional[datetime.datetime] = None, limit: int = 10) -> List[Dict]:
        """Get activities from Garmin Connect."""
        try:
            # Build the params
            params = {"start": 0, "limit": limit}
            
            # Make the request to the Garmin Connect API
            response = garth.connectapi("/activitylist-service/activities/search/activities", params=params)
            return response if isinstance(response, list) else []
        except Exception as e:
            logger.error(f"Error getting activities from Garmin Connect: {e}")
            return []
    
    def upload_fit(self, fit_data: bytes, activity_name: str = None) -> Optional[Dict]:
        """
        Upload a FIT file to Garmin Connect with retry mechanism.
        
        Args:
            fit_data: The binary FIT file data
            activity_name: Optional name for the activity
            
        Returns:
            Dict with upload response or None if all attempts failed
        """
        retries = 0
        last_error = None
        
        while retries <= self.max_retries:
            try:
                if retries > 0:
                    delay = (self.retry_delay * (2 ** (retries - 1))) + random.uniform(0, 2)
                    logger.info(f"Retrying upload (attempt {retries}/{self.max_retries}) after {delay:.2f}s delay...")
                    time.sleep(delay)
                
                with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as temp_file:
                    temp_file.write(fit_data)
                    temp_file_path = temp_file.name
                
                with open(temp_file_path, "rb") as f:
                    uploaded = garth.client.upload(f)
                
                os.unlink(temp_file_path)
                
                logger.info(f"Successfully uploaded activity to Garmin Connect: {uploaded}")
                return uploaded
            
            except Exception as e:
                last_error = e
                retries += 1
                logger.warning(f"Upload attempt {retries} failed with error: {activity_name or "Unknown Activity"}, {len(fit_data)} bytes, {e}")
                
                if "authentication" in str(e).lower() or "unauthorized" in str(e).lower():
                    logger.info("Attempting to re-authenticate before next retry...")
                    try:
                        self.authenticate()
                    except Exception as auth_err:
                        logger.error(f"Re-authentication failed: {auth_err}")

                if retries > self.max_retries:
                    logger.error(f"Failed to upload after {self.max_retries} attempts. Last error: {last_error}")
                    return None
        
        return None

def load_last_sync_date() -> datetime.datetime:
    """Load the last sync date from the JSON file."""
    try:
        if os.path.exists(LAST_SYNC_FILE):
            with open(LAST_SYNC_FILE, "r") as f:
                data = json.load(f)
                return datetime.datetime.fromisoformat(data["last_sync_date"])
        else:
            # Default to 30 days ago if no sync file exists
            return datetime.datetime.now() - datetime.timedelta(days=30)
    except Exception as e:
        logger.error(f"Error loading last sync date: {e}")
        # Default to 30 days ago on error
        return datetime.datetime.now() - datetime.timedelta(days=30)

def save_last_sync_date(sync_date: datetime.datetime) -> None:
    """Save the last sync date to the JSON file."""
    try:
        with open(LAST_SYNC_FILE, "w") as f:
            json.dump({"last_sync_date": sync_date.isoformat()}, f)
    except Exception as e:
        logger.error(f"Error saving last sync date: {e}")

def activities_overlap(start_time1: datetime.datetime, duration1: int, 
                      start_time2: datetime.datetime, duration2: int) -> bool:
    """Check if two activities overlap in time."""
    end_time1 = start_time1 + datetime.timedelta(seconds=duration1)
    end_time2 = start_time2 + datetime.timedelta(seconds=duration2)
    
    buffer = datetime.timedelta(minutes=OVERLAP_BUFFER_MINUTES)
    
    # Check if either activity is contained within the other (with buffer)
    return (
        (start_time1 - buffer <= start_time2 <= end_time1 + buffer) or
        (start_time1 - buffer <= end_time2 <= end_time1 + buffer) or
        (start_time2 - buffer <= start_time1 <= end_time2 + buffer) or
        (start_time2 - buffer <= end_time1 <= end_time2 + buffer)
    )

def main():
    """Main execution function."""
    # Get credentials from environment variables
    igpsport_username = os.environ.get("IGPSPORT_USERNAME")
    igpsport_password = os.environ.get("IGPSPORT_PASSWORD")
    garmin_email = os.environ.get("GARMIN_EMAIL")
    garmin_password = os.environ.get("GARMIN_PASSWORD")
    garmin_domain = os.environ.get("GARMIN_DOMAIN") or "garmin.com"
    
    if not all([igpsport_username, igpsport_password, garmin_email, garmin_password, garmin_domain]):
        logger.error("Missing required environment variables")
        return
    
    # Initialize clients
    igpsport_client = IGPSportClient(igpsport_username, igpsport_password)
    garmin_client = GarminClient(garmin_email, garmin_password, garmin_domain)
    
    # Authenticate with both services
    if not igpsport_client.login():
        logger.error("Failed to authenticate with iGPSport")
        return
    
    if not garmin_client.authenticate():
        logger.error("Failed to authenticate with Garmin")
        return
    
    # Load last sync date
    last_sync_date = load_last_sync_date()
    logger.info(f"Last sync date: {last_sync_date}")
    
    # Get recent activities from Garmin to check for overlap
    garmin_activities = garmin_client.get_activities(limit=20)
    garmin_activity_times = []
    for activity in garmin_activities:
        try:
            start_time = parse(activity.get("startTimeLocal", ""))
            duration = activity.get("duration", 0)
            garmin_activity_times.append((start_time, duration))
        except Exception as e:
            logger.warning(f"Error parsing Garmin activity time: {e}")
    
    # Get activities from iGPSport
    page_no = 1
    page_size = 20
    activities_data = igpsport_client.get_activities(page_no, page_size)
    
    if not activities_data or "rows" not in activities_data:
        logger.error("Failed to get activities from iGPSport")
        return
    
    activities = activities_data["rows"]
    sync_count = 0
    skip_count = 0
    latest_synced_date = None
    
    for activity in activities:
        try:
            # Parse activity start time
            start_time_str = activity.get("startTime", "")
            activity_id = activity.get("rideId")
            # Handle the format like "2024.11.20" which is not ISO format
            # We'll need to convert it to proper datetime
            if "." in start_time_str:
                parts = start_time_str.split(".")
                if len(parts) == 3:
                    year, month, day = parts
                    start_time = datetime.datetime(int(year), int(month), int(day))
                else:
                    logger.warning(f"Invalid date format: {start_time_str}")
                    continue
            else:
                start_time = parse(start_time_str)
            
            # Skip if older than last sync date
            if start_time.date() < last_sync_date.date():
                logger.info(f"Skipping activity {activity_id} from {start_time} (older than last sync)")
                skip_count += 1
                continue
            
            # Get activity detail to get the full start time and duration
            activity_detail = igpsport_client.get_activity_detail(activity_id)
            
            if not activity_detail:
                logger.warning(f"Could not get details for activity {activity_id}")
                continue
            
            # Parse the detailed start time
            detail_start_time = parse(activity_detail.get("startTime", ""))
            detail_duration = activity_detail.get("totalTime", 0)
            
            # Check for overlap with existing Garmin activities
            overlaps = False
            for garmin_start, garmin_duration in garmin_activity_times:
                if activities_overlap(detail_start_time, detail_duration, garmin_start, garmin_duration):
                    logger.info(f"Skipping activity {activity_id} due to time overlap with existing Garmin activity")
                    overlaps = True
                    break
            
            if overlaps:
                skip_count += 1
                continue
            
            # Download the FIT file
            fit_url = activity.get("fitOssPath")
            if not fit_url:
                logger.warning(f"No FIT file URL for activity {activity_id}")
                continue
            
            fit_data = igpsport_client.download_fit_file(fit_url)
            if not fit_data:
                logger.warning(f"Failed to download FIT file for activity {activity_id}")
                continue
            
            # Upload to Garmin
            result = garmin_client.upload_fit(fit_data)
            if result:
                logger.info(f"Successfully uploaded activity {activity_id} to Garmin")
                sync_count += 1

                # Update the latest synced date
                if latest_synced_date is None or detail_start_time > latest_synced_date:
                    latest_synced_date = detail_start_time
                
                # Sleep to avoid rate limiting
                time.sleep(2)
            else:
                logger.warning(f"Failed to upload activity {activity_id} to Garmin after all retry attempts")
        
        except Exception as e:
            logger.error(f"Error processing activity: {e}")
    
    # Save the latest synced activity date instead of current date
    if latest_synced_date and sync_count > 0:
        save_last_sync_date(latest_synced_date)
        logger.info(f"Updated last sync date to: {latest_synced_date}")
    else:
        logger.info("No activities were synced, last sync date remains unchanged")
    
    logger.info(f"Sync completed: {sync_count} activities uploaded, {skip_count} activities skipped")

if __name__ == "__main__":
    main()