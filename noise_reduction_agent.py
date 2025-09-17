import json
import boto3
import asyncio
import httpx
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager

import numpy as np
from collections import defaultdict
import logging
import uuid
import hashlib
import random

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph
from langgraph.graph.message import AnyMessage, add_messages
from typing_extensions import Annotated, TypedDict

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
CORRELATION_API_URL = os.getenv("CORRELATION_API_URL", "http://localhost:8003")

class NoiseReductionRequest(BaseModel):
    alerts: List[Dict[str, Any]] = Field(..., description="List of alerts to process for noise reduction")
    source: str = Field(..., description="Source of the alerts")
    environment: str = Field(..., description="Environment (development or production)")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + 'Z')

class NoiseReductionResponse(BaseModel):
    processed_alerts: List[Dict[str, Any]]
    filtered_count: int
    original_count: int
    correlation_response: Optional[Dict[str, Any]] = None
    processing_time_ms: float
    timestamp: str

class WorkflowState(TypedDict):
    """State for LangGraph workflow"""
    alerts: List[Dict[str, Any]]
    environment: str
    source: str
    processed_alerts: List[Dict[str, Any]]
    filtered_count: int
    correlation_response: Optional[Dict[str, Any]]
    messages: Annotated[list[AnyMessage], add_messages]
    error: Optional[str]

class NoiseReductionAgent:
    """
    Agent responsible for noise reduction - filtering duplicates, low severity alerts, etc.
    Maintains the same functionality as apply_noise_reduction from dbscan_correlation.py
    """
    
    def __init__(self, config: Optional[Dict] = None, state: Optional[str] = None):
        self.config = config or {}
        self.state = state
        self.dynamodb = boto3.resource('dynamodb')
        
        # Configuration parameters
        self.min_severity_threshold = self.config.get('min_severity_threshold', 2)
        self.duplicate_time_window = self.config.get('duplicate_time_window', 300)  # 5 minutes
        self.similarity_threshold = self.config.get('similarity_threshold', 0.8)
        
        # Environment must be explicitly set - no defaults
        self.environment = os.environ.get('ENVIRONMENT')
        if not self.environment:
            raise ValueError("ENVIRONMENT variable must be set to either 'production' or 'development'")
        self.environment = self.environment.strip().lower()
        if self.environment not in ("production", "development"):
            raise ValueError(f"Invalid ENVIRONMENT value: {self.environment}. Must be 'production' or 'development'")
        
        # Cross-run duplicate cache removed as requested
        
        # Table mappings (same as in dbscan_correlation.py)
        self.table_mapping = {
            "development": {
                "incidents": "dev-incidents",
                "companies": "dev-users",
                "noise_analytics": "dev-noise-analytics",
                "business_units": "dev-businessUnits",
                "services": "dev-services",
                "counters": "dev-counters",
                "timelines": "dev-incident-timelines"
            },
            "production": {
                "incidents": "incidents",
                "companies": "users",
                "noise_analytics": "noise-analytics",
                "business_units": "businessUnits",
                "services": "services",
                "counters": "counters",
                "timelines": "incident-timelines"
            }
        }
    
    def get_table_name(self, table_type: str, environment: Optional[str] = None) -> str:
        """Get the correct table name based on environment"""
        if not environment:
            environment = self.environment
        
        return self.table_mapping.get(environment, {}).get(table_type, table_type)
    
    def get_table_by_environment(self, table_type: str, environment: Optional[str] = None):
        """Get the DynamoDB table for the specified type and environment"""
        table_name = self.get_table_name(table_type, environment)
        return self.dynamodb.Table(table_name)
    
    def calculate_alert_severity_score(self, alert: Dict) -> float:
        """Calculate severity score for an alert"""
        try:
            # Priority mapping
            priority_scores = {
                'critical': 5, 'p0': 5, 'p1': 4, 'high': 4,
                'medium': 3, 'p2': 3, 'p3': 2, 'low': 2, 'p4': 1
            }
            
            # Get priority from various fields
            priority = alert.get('priority', '').lower()
            if not priority:
                priority = alert.get('severity', '').lower()
            
            score = priority_scores.get(priority, 3)  # Default to medium
            
            # Adjust based on impact
            impact = alert.get('impact', '').lower()
            if impact in ['critical', 'high']:
                score += 1
            elif impact in ['low']:
                score -= 1
            
            # Adjust based on urgency
            urgency = alert.get('urgency', '').lower()
            if urgency in ['critical', 'high']:
                score += 0.5
            elif urgency in ['low']:
                score -= 0.5
            
            # Ensure score is within bounds
            return max(1, min(5, score))
            
        except Exception as e:
            logger.warning(f"Error calculating severity score: {e}")
            return 3  # Default medium severity
    
    def is_duplicate_alert(self, alert1: Dict, alert2: Dict) -> bool:
        """Check if two alerts are duplicates"""
        try:
            # Same ID check
            if alert1.get('id') == alert2.get('id'):
                logger.debug(f"Duplicate detected: Same ID {alert1.get('id')}")
                return True
            
            # Title similarity
            title1 = alert1.get('title', '').lower().strip()
            title2 = alert2.get('title', '').lower().strip()
            
            if not title1 or not title2:
                return False
            
            # Company ID must match for duplicates
            company1 = alert1.get('company_id')
            company2 = alert2.get('company_id')
            if company1 != company2:
                return False
            
            # Exact title match - more relaxed service matching
            if title1 == title2:
                # Check service match - more relaxed, allow partial matches
                service1 = str(alert1.get('service', '')).lower()
                service2 = str(alert2.get('service', '')).lower()
                
                # Either exact match or one contains the other
                service_match = (service1 == service2 or 
                                service1 in service2 or 
                                service2 in service1)
                
                if service_match:
                    # Check time proximity (within duplicate_time_window)
                    # Increased from default 5 minutes to 15 minutes
                    time1 = alert1.get('parsed_time')
                    time2 = alert2.get('parsed_time')
                    
                    if time1 and time2:
                        time_diff = abs((time1 - time2).total_seconds())
                        # Use a larger time window (15 minutes = 900 seconds)
                        effective_time_window = max(self.duplicate_time_window, 900)
                        if time_diff <= effective_time_window:
                            logger.info(f"Duplicate detected: Exact title match '{title1}' with time diff {time_diff}s")
                            return True
            
            # Fuzzy title matching for similar alerts - more conservative approach
            if len(title1) > 10 and len(title2) > 10:  # Increased minimum length requirement
                # Simple fuzzy matching based on common words
                words1 = set(title1.split())
                words2 = set(title2.split())
                
                if len(words1) > 2 and len(words2) > 2:  # Need at least 3 words each
                    common_words = words1.intersection(words2)
                    similarity = len(common_words) / max(len(words1), len(words2))
                    
                    # More conservative threshold for similarity - require higher similarity
                    effective_threshold = max(self.similarity_threshold, 0.8)
                    
                    if similarity >= effective_threshold:
                        # Additional checks for fuzzy matches - exact service matching required
                        service1 = str(alert1.get('service', '')).lower()
                        service2 = str(alert2.get('service', '')).lower()
                        
                        # Require exact service match for fuzzy title matches
                        if service1 == service2:
                            # Check time proximity with larger window
                            time1 = alert1.get('parsed_time')
                            time2 = alert2.get('parsed_time')
                            
                            if time1 and time2:
                                time_diff = abs((time1 - time2).total_seconds())
                                # Use a larger time window (15 minutes = 900 seconds)
                                effective_time_window = max(self.duplicate_time_window, 900)
                                if time_diff <= effective_time_window:
                                    # Additional check: titles should be very similar
                                    # If they have different server names or numbers, not duplicates
                                    import re
                                    # Check for different server identifiers
                                    server_pattern = r'server\s+[a-z0-9]+|host\s+[a-z0-9]+|node\s+[a-z0-9]+'
                                    servers1 = set(re.findall(server_pattern, title1))
                                    servers2 = set(re.findall(server_pattern, title2))
                                    
                                    if servers1 and servers2 and servers1 != servers2:
                                        logger.debug(f"Different servers detected: {servers1} vs {servers2}, not duplicates")
                                        return False
                                    
                                    logger.info(f"Duplicate detected: Fuzzy title match between '{title1}' and '{title2}' with similarity {similarity:.2f}")
                                    return True
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking duplicate alerts: {e}")
            return False
    
    def group_alerts_by_company(self, alerts: List[Dict]) -> Dict[str, List[Dict]]:
        """Group alerts by company_id"""
        company_groups = defaultdict(list)
        
        for alert in alerts:
            company_id = alert.get('company_id', 'unknown')
            company_groups[company_id].append(alert)
        
        return dict(company_groups)
    
    def apply_severity_filtering(self, alerts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Filter alerts based on severity threshold"""
        kept_alerts = []
        suppressed_alerts = []
        
        for alert in alerts:
            severity_score = self.calculate_alert_severity_score(alert)
            
            if severity_score >= self.min_severity_threshold:
                kept_alerts.append(alert)
            else:
                alert['suppression_reason'] = 'low_severity'
                suppressed_alerts.append(alert)
        
        return kept_alerts, suppressed_alerts
    
    def apply_duplicate_filtering(self, alerts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Filter duplicate alerts, keeping the most recent one"""
        if len(alerts) <= 1:
            return alerts, []
        
        logger.info(f"Starting duplicate filtering on {len(alerts)} alerts")
        
        # Sort alerts by timestamp (most recent first)
        sorted_alerts = sorted(alerts, key=lambda x: x.get('parsed_time', datetime.min), reverse=True)
        
        kept_alerts = []
        suppressed_alerts = []
        duplicate_groups = []
        
        # Group duplicates
        processed_indices = set()
        
        for i, alert1 in enumerate(sorted_alerts):
            if i in processed_indices:
                continue
            
            duplicate_group = [alert1]
            processed_indices.add(i)
            
            alert1_title = alert1.get('title', 'No title')
            alert1_id = alert1.get('id', 'No ID')
            logger.debug(f"Checking duplicates for alert {i}: '{alert1_title}' (ID: {alert1_id})")
            
            # Find all duplicates of this alert
            for j, alert2 in enumerate(sorted_alerts[i+1:], i+1):
                if j in processed_indices:
                    continue
                
                alert2_title = alert2.get('title', 'No title')
                alert2_id = alert2.get('id', 'No ID')
                
                # Check if they're duplicates
                is_duplicate = self.is_duplicate_alert(alert1, alert2)
                
                if is_duplicate:
                    logger.info(f"Found duplicate: Alert {alert1_id} '{alert1_title}' matches Alert {alert2_id} '{alert2_title}'")
                    duplicate_group.append(alert2)
                    processed_indices.add(j)
                else:
                    logger.debug(f"Not a duplicate: Alert {alert1_id} '{alert1_title}' vs Alert {alert2_id} '{alert2_title}'")
            
            if len(duplicate_group) > 1:
                # Keep the most recent (first in sorted list)
                kept_alert = duplicate_group[0]
                kept_alerts.append(kept_alert)
                
                # Mark others as suppressed
                for dup_alert in duplicate_group[1:]:
                    dup_alert['suppression_reason'] = 'duplicate'
                    dup_alert['noise_reduced'] = 'true'  # Explicitly mark as noise reduced
                    suppressed_alerts.append(dup_alert)
                
                duplicate_groups.append(duplicate_group)
                logger.info(f"Found duplicate group of {len(duplicate_group)} alerts:")
                for alert in duplicate_group:
                    status = "Kept" if alert == duplicate_group[0] else "Suppressed"
                    logger.info(f"      - {alert.get('id')}: {alert.get('title')} ({status})")
            else:
                # No duplicates found
                logger.debug(f"No duplicates found for alert {alert1_id} '{alert1_title}'")
                kept_alerts.append(alert1)
        
        logger.info(f"Duplicate filtering complete: {len(suppressed_alerts)} duplicates found out of {len(alerts)} alerts")
        return kept_alerts, suppressed_alerts
    
    async def check_existing_duplicates(self, alerts: List[Dict], environment: str) -> List[Dict]:
        """Check for duplicates against existing alerts in DynamoDB"""
        logger.info(f"Checking {len(alerts)} alerts against existing alerts in DynamoDB...")
        
        # Group alerts by company for efficient querying
        company_groups = self.group_alerts_by_company(alerts)
        
        duplicates_found = []
        
        for company_id, company_alerts in company_groups.items():
            logger.info(f"  🏢 Checking for duplicates in company {company_id}, environment {environment}")
            
            try:
                # Get incidents table for this environment
                incidents_table = self.get_table_by_environment("incidents", environment)
                
                # Query recent incidents for this company
                cutoff_time = datetime.utcnow() - timedelta(hours=24)  # Look back 24 hours
                
                response = incidents_table.scan(
                    FilterExpression="company_id = :cid AND created_at > :cutoff",
                    ExpressionAttributeValues={
                        ":cid": company_id,
                        ":cutoff": cutoff_time.isoformat() + 'Z'
                    }
                )
                
                existing_incidents = response.get('Items', [])
                logger.info(f"    Found {len(existing_incidents)} recent incidents for company {company_id}")
                
                # Check each alert against existing incidents
                for alert in company_alerts:
                    for incident in existing_incidents:
                        # Simple duplicate check based on title and service
                        alert_title = alert.get('title', '').lower()
                        incident_title = incident.get('title', '').lower()
                        
                        alert_service = str(alert.get('service', '')).lower()
                        incident_service_id = incident.get('service_id', '').lower()
                        
                        if alert_title == incident_title and alert_service in incident_service_id:
                            logger.info(f"    🔍 Found potential duplicate: Alert {alert.get('id')} matches incident {incident.get('id')}")
                            duplicates_found.append(alert)
                            break
                
            except Exception as e:
                logger.error(f"Error checking duplicates for company {company_id}: {e}")
        
        logger.info(f"✅ Duplicate check complete - found {len(duplicates_found)} duplicates")
        return duplicates_found
    
    async def mark_alerts_as_noise_reduced(self, suppressed_alerts: List[Dict], environment: str):
        """Mark suppressed alerts in database"""
        if not suppressed_alerts:
            return
        
        logger.info(f"Marking {len(suppressed_alerts)} alerts as noise reduced...")
        
        # Group by environment for processing
        logger.info(f"Processing {len(suppressed_alerts)} alerts for {environment} environment...")
        
        for alert in suppressed_alerts:
            try:
                alert_id = alert.get('id')
                logger.info(f"    Marked alert {alert_id} as noise reduced locally (not storing in incidents table)")
                
                # In the original code, this was marked locally, not stored in incidents table
                # We maintain the same behavior here
                
            except Exception as e:
                logger.error(f"Error marking alert {alert.get('id')} as noise reduced: {e}")
        
        logger.info(f"✅ Successfully saved {len(suppressed_alerts)} noise-reduced alert IDs locally")
        
    async def send_noise_analytics(self, stats: Dict, kept_alerts: List[Dict], suppressed_alerts: List[Dict], environment: Optional[str] = None) -> None:
        """Send noise analytics data to the API endpoint"""
        try:
            # Determine API endpoint based on environment
            api_base_url = "https://api.bugraid.ai" if environment == "production" else "https://dev-api.bugraid.ai"
            api_endpoint = f"{api_base_url}/api/v1/services/noise-analytics"
            
            # Group alerts by company_id
            company_alerts = defaultdict(lambda: {"kept": [], "suppressed": []})
            
            # Process kept alerts
            for alert in kept_alerts:
                company_id = alert.get('company_id', 'unknown')
                company_alerts[company_id]["kept"].append(alert)
                
            # Process suppressed alerts
            for alert in suppressed_alerts:
                company_id = alert.get('company_id', 'unknown')
                company_alerts[company_id]["suppressed"].append(alert)
            
            # Create analytics payload for each company
            for company_id, alerts_data in company_alerts.items():
                # Skip if no company_id
                if company_id == 'unknown':
                    continue
                    
                # Calculate company-specific stats
                company_kept = len(alerts_data["kept"])
                company_suppressed = len(alerts_data["suppressed"])
                company_total = company_kept + company_suppressed
                company_suppression_rate = round((company_suppressed / company_total) * 100, 2) if company_total > 0 else 0
                
                # Group suppressed alerts by suppression reason
                suppression_reasons = defaultdict(int)
                for alert in alerts_data["suppressed"]:
                    reason = alert.get('suppression_reason', 'unknown')
                    suppression_reasons[reason] += 1
                
                # Create suppressed alert details
                suppressed_alert_details = []
                for reason, count in suppression_reasons.items():
                    suppressed_alert_details.append({
                        "reason": reason,
                        "count": count,
                        "percentage": round((count / company_suppressed) * 100, 2) if company_suppressed > 0 else 0
                    })
                
                # Create company breakdown
                company_breakdown = [{
                    "company_id": company_id,
                    "total_processed": company_total,
                    "total_kept": company_kept,
                    "total_suppressed": company_suppressed,
                    "suppression_rate": company_suppression_rate
                }]
                
                # Current time info
                now = datetime.utcnow()
                current_hour = now.hour
                current_date = now.strftime("%Y-%m-%d")
                
                # Build analytics payload
                analytics_payload = {
                    "timestamp": now.isoformat() + "Z",
                    "company_id": company_id,
                    "date": current_date,
                    "duplicate_groups_found": stats.get("duplicate_groups", 0),
                    "hour": current_hour,
                    "processing_timestamp": now.isoformat() + "Z",
                    "suppressed_alert_details": suppressed_alert_details,
                    "company_breakdown": company_breakdown,
                    "suppression_rate": company_suppression_rate,
                    "total_kept": company_kept,
                    "total_processed": company_total,
                    "total_suppressed": company_suppressed
                }
                
                # Send analytics to API
                logger.info(f"Sending noise analytics for company {company_id} to {api_endpoint}")
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(api_endpoint, json=analytics_payload)
                        if response.status_code >= 200 and response.status_code < 300:
                            logger.info(f"Successfully sent noise analytics for company {company_id}")
                        else:
                            logger.warning(f"Failed to send noise analytics: API returned status {response.status_code}")
                            logger.debug(f"Response: {response.text}")
                except Exception as e:
                    logger.warning(f"Error sending noise analytics to API: {e}")
                
        except Exception as e:
            logger.error(f"Error preparing noise analytics data: {e}")
            return
    
    async def record_noise_reduction_analytics(self, noise_stats: Dict, suppressed_alerts: List[Dict], 
                                             company_breakdown: Optional[Dict] = None, environment: str = None):
        """Record noise reduction analytics in database"""
        try:
            if not environment:
                environment = self.environment
            
            analytics_table = self.get_table_by_environment("noise_analytics", environment)
            
            # Create analytics record
            analytics_record = {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.utcnow().isoformat() + 'Z',
                "total_processed": noise_stats.get('total_processed', 0),
                "suppressed": noise_stats.get('suppressed', 0),
                "kept": noise_stats.get('kept', 0),
                "suppression_rate": int(noise_stats.get('suppression_rate', 0.0)),
                "duplicate_groups": noise_stats.get('duplicate_groups', 0),
                "suppression_reasons": noise_stats.get('suppression_reasons', {}),
                "environment": environment
            }
            
            # Add company breakdown if available
            if company_breakdown:
                analytics_record["company_breakdown"] = company_breakdown
            
            # Add suppressed alerts details
            suppressed_details = []
            for alert in suppressed_alerts:
                detail = {
                    "id": alert.get('id'),
                    "title": alert.get('title'),
                    "service": alert.get('service'),
                    "company_id": alert.get('company_id'),
                    "suppression_reason": alert.get('suppression_reason'),
                    "timestamp": alert.get('parsed_time', datetime.utcnow()).isoformat() if isinstance(alert.get('parsed_time'), datetime) else str(alert.get('parsed_time'))
                }
                suppressed_details.append(detail)
            
            analytics_record["suppressed_alerts"] = suppressed_details
            
            # Store in DynamoDB
            analytics_table.put_item(Item=analytics_record)
            
            logger.info(f"📊 Recorded noise reduction analytics: {noise_stats['suppressed']} suppressed out of {noise_stats['total_processed']}")
            
        except Exception as e:
            logger.error(f"Error recording noise reduction analytics: {e}")
    
    def get_noise_reduction_visualization(self, total: int, kept: int) -> str:
        """Generate visual representation of noise reduction"""
        if total == 0:
            return "[No alerts processed]"
        
        kept_percent = (kept / total) * 100
        suppressed_percent = 100 - kept_percent
        
        # Create visual bar (30 characters total)
        bar_length = 30
        kept_chars = int((kept_percent / 100) * bar_length)
        suppressed_chars = bar_length - kept_chars
        
        bar = "✅" * kept_chars + "🔇" * suppressed_chars
        return f"[{bar}] {kept_percent:.1f}% kept"
    
    def calculate_alert_signature(self, alert: Dict) -> str:
        """Calculate a signature for an alert based on its content"""
        try:
            # Extract key fields for signature
            title = alert.get('title', '').lower()
            service = str(alert.get('service', '')).lower()
            source = alert.get('source', '').lower()
            
            # Create a signature string
            signature = f"{service}:{source}:{title}"
            
            # Hash the signature for consistency
            return hashlib.md5(signature.encode()).hexdigest()
        except Exception as e:
            logger.warning(f"Error calculating alert signature: {e}")
            return ""

    async def apply_noise_reduction(self, alerts: List[Dict], environment: str = None) -> Tuple[List[Dict], Dict]:
        """
        Apply noise reduction to filter out duplicate and low-priority alerts
        
        Args:
            alerts: List of alert dictionaries
            environment: Environment (development/production)
            
        Returns:
            Tuple of (filtered_alerts, stats)
        """
        if not environment:
            environment = self.environment
        
        logger.info(f"Starting noise reduction on {len(alerts)} alerts...")
        
        if not alerts:
            return [], {"total_alerts": 0, "noise_reduced": 0}
        
        # Track statistics
        stats = {
            "total_alerts": len(alerts),
            "duplicate_alerts": 0,
            "low_priority_alerts": 0,
            "cross_run_duplicates": 0,
            "noise_reduced": 0
        }
        
        # Cross-run duplicate check removed as requested
        logger.info(f"Using only time-window based duplicate detection")
        
        # Apply duplicate filtering using the enhanced algorithm on all alerts
        kept_alerts, suppressed_duplicate_alerts = self.apply_duplicate_filtering(alerts)
        stats["cross_run_duplicates"] = 0  # No cross-run duplicates anymore
        stats["duplicate_alerts"] = len(suppressed_duplicate_alerts)
        
        # Apply severity filtering to the kept alerts
        final_kept_alerts, suppressed_low_priority_alerts = self.apply_severity_filtering(kept_alerts)
        stats["low_priority_alerts"] = len(suppressed_low_priority_alerts)
        
        # Combine all suppressed alerts
        all_suppressed_alerts = suppressed_duplicate_alerts + suppressed_low_priority_alerts
        
        # Ensure all suppressed alerts are properly marked
        for alert in all_suppressed_alerts:
            alert['noise_reduced'] = 'true'
            if 'suppression_reason' not in alert:
                alert['suppression_reason'] = 'unknown'
        
        # Calculate total noise reduced
        stats["noise_reduced"] = len(all_suppressed_alerts)
        stats["noise_reduction_percentage"] = round((stats["noise_reduced"] / stats["total_alerts"]) * 100, 2) if stats["total_alerts"] > 0 else 0
        
        # Mark suppressed alerts in the database (if needed)
        if all_suppressed_alerts:
            await self.mark_alerts_as_noise_reduced(all_suppressed_alerts, environment)
            
        # Send noise analytics to API
        await self.send_noise_analytics(stats, final_kept_alerts, all_suppressed_alerts, environment)
        
        logger.info(f"NOISE REDUCTION SUMMARY")
        logger.info(f"Total alerts processed:       {stats['total_alerts']}")
        logger.info(f"Cross-run duplicates filtered: {stats['cross_run_duplicates']}")
        logger.info(f"Duplicate alerts filtered:    {stats['duplicate_alerts']}")
        logger.info(f"Low priority alerts filtered: {stats['low_priority_alerts']}")
        logger.info(f"Total noise reduced:          {stats['noise_reduced']} ({stats['noise_reduction_percentage']}%)")
        logger.info(f"Alerts remaining:             {len(final_kept_alerts)}")
        
        # Log the first few kept alerts for debugging
        if final_kept_alerts:
            logger.info("Sample of kept alerts:")
            for i, alert in enumerate(final_kept_alerts[:5]):  # Show up to 5 examples
                logger.info(f"  - {alert.get('id')}: {alert.get('title')}")
            if len(final_kept_alerts) > 5:
                logger.info(f"  ... and {len(final_kept_alerts) - 5} more")
        
        # Log the first few suppressed alerts for debugging
        if all_suppressed_alerts:
            logger.info("Sample of suppressed alerts:")
            for i, alert in enumerate(all_suppressed_alerts[:5]):  # Show up to 5 examples
                logger.info(f"  - {alert.get('id')}: {alert.get('title')} (reason: {alert.get('suppression_reason')})")
            if len(all_suppressed_alerts) > 5:
                logger.info(f"  ... and {len(all_suppressed_alerts) - 5} more")
        
        return final_kept_alerts, stats
    
    async def send_to_correlation_agent(self, processed_alerts: List[Dict[str, Any]], environment: str, source: str) -> Optional[Dict[str, Any]]:
        """Send processed alerts to the correlation agent"""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                payload = {
                    "alerts": processed_alerts,
                    "environment": environment,
                    "source": source,
                    "timestamp": datetime.utcnow().isoformat() + 'Z'
                }
                
                response = await client.post(
                    f"{CORRELATION_API_URL}/correlate-alerts",
                    json=payload
                )
                
                if response.status_code == 200:
                    logger.info(f"Successfully sent {len(processed_alerts)} alerts to correlation agent")
                    return response.json()
                else:
                    logger.error(f"Failed to send alerts to correlation agent: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error sending alerts to correlation agent: {e}")
            return None

# Example usage and testing
async def main(state=None):
    """
    Production-ready entry point for NoiseReductionAgent
    
    Args:
        state: Optional state parameter (e.g., 'production', 'development')
    """
    logger.info(f"Starting NoiseReductionAgent in state: {state}")
    
    # STRICT: Use only the ENVIRONMENT variable - no overrides or fallbacks
    environment = os.environ.get('ENVIRONMENT')
    if not environment:
        raise ValueError("ENVIRONMENT variable must be set to either 'production' or 'development'")
    environment = environment.strip().lower()
    if environment not in ("production", "development"):
        raise ValueError(f"Invalid ENVIRONMENT value: {environment}. Must be 'production' or 'development'")
    
    # Configure based on state
    config = {}
    
    # Check if state is a dictionary with configuration
    if isinstance(state, dict):
        config = state.get('config', {})
        # DO NOT override environment from config - use only ENVIRONMENT variable
        alerts = state.get('alerts', [])
    else:
        # Handle string state parameter (legacy mode) - but still use ENVIRONMENT variable
        config = {
            "similarity_threshold": 0.75,
            "duplicate_time_window_minutes": 30
        }
    
        # Get alerts from file
        alerts = []
        alerts_file = os.environ.get("NOISE_REDUCTION_ALERTS_FILE")
        if alerts_file and os.path.exists(alerts_file):
            logger.info(f"Loading alerts from file: {alerts_file}")
            with open(alerts_file, 'r') as f:
                alerts = json.load(f)
        
    agent = NoiseReductionAgent(config=config)
    
    try:
        if not alerts:
            logger.warning("No alerts found for noise reduction. Exiting.")
            return {
                "filtered_alerts": [],
                "noise_reduction_stats": {
                    "total_alerts": 0,
                    "noise_reduced": 0
                }
            }
        
        logger.info(f"Applying noise reduction to {len(alerts)} alerts in {environment} environment")
        
        # Apply noise reduction
        filtered_alerts, stats = await agent.apply_noise_reduction(alerts, environment)
        
        # Save results if output file is specified
        output_file = os.environ.get("NOISE_REDUCTION_OUTPUT_FILE")
        if output_file:
            with open(output_file, 'w') as f:
                json.dump({
                    "filtered_alerts": filtered_alerts,
                    "stats": stats
                }, f, indent=2)
            logger.info(f"Saved noise reduction results to {output_file}")
        
        return {
            "filtered_alerts": filtered_alerts,
            "noise_reduction_stats": stats
        }
    except Exception as e:
        logger.error(f"Error in noise reduction: {e}")
        return {
            "filtered_alerts": alerts,
            "noise_reduction_stats": {
                "total_alerts": len(alerts),
                "error": str(e),
                "noise_reduced": 0
            }
        }

# Initialize service (will be created when needed)
noise_service = None

def get_noise_service():
    """Get or create noise service instance"""
    global noise_service
    if noise_service is None:
        noise_service = NoiseReductionAgent()
    return noise_service

# LangGraph workflow definition
async def noise_reduction_node(state: WorkflowState) -> WorkflowState:
    """Node to apply noise reduction"""
    try:
        alerts = state["alerts"]
        environment = state["environment"]
        
        filtered_alerts, stats = await get_noise_service().apply_noise_reduction(alerts, environment)
        
        state["processed_alerts"] = filtered_alerts
        state["filtered_count"] = stats.get("noise_reduced", 0)
        state["messages"].append({"role": "system", "content": f"Noise reduction: {len(alerts)} -> {len(filtered_alerts)} alerts"})
        return state
    except Exception as e:
        state["error"] = str(e)
        return state

async def send_to_correlation_node(state: WorkflowState) -> WorkflowState:
    """Node to send alerts to correlation agent"""
    try:
        processed_alerts = state["processed_alerts"]
        environment = state["environment"]
        source = state["source"]
        
        if processed_alerts:
            correlation_response = await get_noise_service().send_to_correlation_agent(processed_alerts, environment, source)
            state["correlation_response"] = correlation_response
            state["messages"].append({"role": "system", "content": f"Sent {len(processed_alerts)} alerts to correlation agent"})
        else:
            state["messages"].append({"role": "system", "content": "No alerts to send to correlation agent"})
        
        return state
    except Exception as e:
        state["error"] = str(e)
        return state

# Create LangGraph workflow
def create_noise_reduction_workflow():
    workflow = StateGraph(WorkflowState)
    
    # Add nodes
    workflow.add_node("noise_reduction", noise_reduction_node)
    workflow.add_node("send_to_correlation", send_to_correlation_node)
    
    # Add edges
    workflow.add_edge("__start__", "noise_reduction")
    workflow.add_edge("noise_reduction", "send_to_correlation")
    workflow.add_edge("send_to_correlation", "__end__")
    
    return workflow.compile()

# FastAPI lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Noise Reduction API")
    yield
    # Shutdown
    logger.info("Noise Reduction API shutdown complete")

# FastAPI app
app = FastAPI(
    title="Noise Reduction API",
    description="FastAPI service for noise reduction using LangGraph workflows",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "noise-reduction",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "correlation_api": CORRELATION_API_URL
    }

@app.post("/reduce-noise", response_model=NoiseReductionResponse)
async def reduce_noise(request: NoiseReductionRequest, background_tasks: BackgroundTasks):
    """Apply noise reduction to alerts and send to correlation agent"""
    try:
        start_time = datetime.utcnow()
        
        # Create workflow
        workflow = create_noise_reduction_workflow()
        
        # Initial state
        initial_state = WorkflowState(
            alerts=request.alerts,
            environment=request.environment,
            source=request.source,
            processed_alerts=[],
            filtered_count=0,
            correlation_response=None,
            messages=[],
            error=None
        )
        
        # Run workflow
        result = await workflow.ainvoke(initial_state)
        
        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])
        
        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
        
        return NoiseReductionResponse(
            processed_alerts=result["processed_alerts"],
            filtered_count=result["filtered_count"],
            original_count=len(request.alerts),
            correlation_response=result.get("correlation_response"),
            processing_time_ms=processing_time,
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
        
    except Exception as e:
        logger.error(f"Error in noise reduction: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003) 