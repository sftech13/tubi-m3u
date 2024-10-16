import requests
from bs4 import BeautifulSoup
import json
import re
import xml.etree.ElementTree as ET
import os
from urllib.parse import unquote
from urllib.parse import urlparse, urlunparse
from datetime import datetime
import unicodedata
import argparse
import logging
import ssl
import urllib3

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Argument parser for dynamic input
parser = argparse.ArgumentParser(description='Scrape Tubi TV data.')
parser.add_argument('--countries', type=str, nargs='+', default=['US'], help='List of country codes.')
args = parser.parse_args()

# Setting up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

countries = args.countries

MAX_RETRIES = 10


def get_proxies(country_code):
    """
    Fetch a list of proxies for a specific country from ProxyScrape.
    Args:
        country_code (str): The country code for which to fetch proxies.
    Returns:
        list: A list of proxies in the format 'socks4://ip:port'.
    """
    url = f"https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=10000&country={country_code}&ssl=all&anonymity=elite"
    response = requests.get(url, verify=False)  # Disable verification for this request
    if response.status_code == 200:
        proxy_list = response.text.splitlines()  # Split the response into individual lines
        return [f"socks4://{proxy}" for proxy in proxy_list]  # Format each proxy as 'socks4://ip:port'
    else:
        logging.warning(f"Failed to fetch proxies for {country_code}. Status code: {response.status_code}")
        return []


def fetch_channel_list(proxy):
    """
    Fetch the channel list from Tubi using a specific proxy.
    Args:
        proxy (str): Proxy to use for the request (format: "protocol://ip:port").
    Returns:
        list: JSON data of the channel list or an empty list if fetch fails.
    """
    url = "https://tubitv.com/live"
    try:
        # Attempt to fetch the URL using the proxy
        response = requests.get(url, proxies={"http": proxy, "https": proxy}, verify=False, timeout=10)
        response.encoding = 'utf-8'  # Force UTF-8 encoding for the response
        if response.status_code != 200:
            logging.warning(f"Failed to fetch data from {url} using proxy {proxy}. Status code: {response.status_code}")
            return []

        html_content = response.content.decode('utf-8', errors='replace')  # Decode content using UTF-8 explicitly
        html_content = html_content.replace('�', 'ñ')  # Replace common problematic characters manually

        # Parse the HTML content with BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")

        # Find all <script> tags and look for the one containing window.__data
        script_tags = soup.find_all("script")
        target_script = None
        for script in script_tags:
            if script.string and script.string.strip().startswith("window.__data"):
                target_script = script.string
                break

        if not target_script:
            logging.error("Error: Could not locate the JSON-like data in the page.")
            logging.debug(f"Logging response content for debugging:\n{html_content[:1000]}...")  # Log the response content for analysis
            return []

        # Extract JSON-like data from the JavaScript code
        start_index = target_script.find("{")
        end_index = target_script.rfind("}") + 1
        json_string = target_script[start_index:end_index]

        # Normalize the JSON string to ensure correct character encoding
        json_string = json_string.encode('utf-8', errors='replace').decode('utf-8')

        # Clean problematic JavaScript constructs in the JSON-like data
        json_string = json_string.replace('undefined', 'null')  # Replace undefined with null
        json_string = re.sub(r'new Date\("([^\"]*)"\)', r'"\1"', json_string)  # Replace new Date() with date string

        logging.debug(f"Extracted JSON-like data (first 500 chars): {json_string[:500]}...")  # Debugging: print part of the data

        data = json.loads(json_string)
        logging.info("Successfully decoded JSON data!")  # Debugging: confirm successful decoding
        return data
    except requests.exceptions.SSLError as ssl_error:
        logging.error(f"SSL error with proxy {proxy}: {ssl_error}")
        return []  # Skip this proxy due to SSL error
    except requests.exceptions.ConnectionError as conn_error:
        logging.error(f"Connection error with proxy {proxy}: {conn_error}")
        return []  # Skip this proxy due to connection issues
    except requests.RequestException as e:
        logging.error(f"Error fetching data using proxy {proxy}: {e}")
        return []


def create_group_mapping(json_data):
    group_mapping = {}

    json_data = [json_data] if not isinstance(json_data, list) else json_data

    for item in json_data:
        content_ids_by_container = item.get('epg', {}).get('contentIdsByContainer', {})
        for container_key, container_list in content_ids_by_container.items():
            for category in container_list:
                group_name = category.get('name', 'Other')
                for content_id in category.get('contents', []):
                    group_mapping[str(content_id)] = group_name  # Map content_id to its group name

    return group_mapping


def fetch_epg_data(channel_list):
    epg_data = []
    group_size = 150
    grouped_ids = [channel_list[i:i + group_size] for i in range(0, len(channel_list), group_size)]

    for group in grouped_ids:
        url = "https://tubitv.com/oz/epg/programming"
        params = {"content_id": ','.join(map(str, group))}
        try:
            response = requests.get(url, params=params, verify=False, timeout=10)

            if response.status_code != 200:
                logging.warning(f"Failed to fetch EPG data for group {group}. Status code: {response.status_code}")
                continue

            try:
                epg_json = response.json()
                epg_data.extend(epg_json.get('rows', []))
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding EPG JSON: {e}")
        except requests.RequestException as e:
            logging.error(f"Error fetching EPG data using proxy: {e}")

    return epg_data


def save_file(content, filename):
    script_directory = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(script_directory, filename)

    try:
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(content)
        logging.info(f"File saved successfully: {file_path}")
    except Exception as e:
        logging.error(f"Failed to save file {file_path}: {e}")


def save_epg_to_file(tree, filename):
    script_directory = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(script_directory, filename)

    try:
        tree.write(file_path, encoding='utf-8', xml_declaration=True)
        logging.info(f"EPG XML file saved successfully: {file_path}")
    except Exception as e:
        logging.error(f"Failed to save EPG XML file {file_path}: {e}")


def main():
    for country in countries:
        proxies = get_proxies(country)
        if not proxies:
            logger.warning(f"No proxies found for country {country}. Skipping...")
            continue

        data_fetched = False
        retries = 0
        for proxy in proxies:
            if retries >= MAX_RETRIES:
                logger.error(f"Reached maximum retry limit for country {country}")
                break

            logger.info(f"Trying proxy {proxy} for country {country}...")
            json_data = fetch_channel_list(proxy)
            if json_data:
                logger.info(f"Successfully fetched data using proxy {proxy} for country {country}")

                # Extract channel list and EPG data
                channel_list = extract_channel_list(json_data)
                logger.info(f"Channel List Length: {len(channel_list)}")

                if not channel_list:
                    logger.error("Channel list is empty, skipping EPG creation.")
                    continue

                epg_data = fetch_epg_data(channel_list)
                logger.info(f"EPG Data Length: {len(epg_data)}")

                if not epg_data:
                    logger.error("EPG data is empty, skipping EPG creation.")
                    retries += 1
                    continue

                # Create the group mapping using the full JSON data
                group_mapping = create_group_mapping(json_data)

                # Create M3U playlist and EPG files
                logger.info("Creating M3U Playlist...")
                m3u_playlist = create_m3u_playlist(epg_data, group_mapping, country.lower())
                logger.info("Creating EPG XML...")
                epg_tree = create_epg_xml(epg_data)

                # Save files with appended country code
                logger.info("Saving M3U Playlist...")
                save_file(m3u_playlist, f"tubi_playlist_{country.lower()}.m3u")
                logger.info("Saving EPG XML...")
                save_epg_to_file(epg_tree, f"tubi_epg_{country.lower()}.xml")

                data_fetched = True
                break  # Stop trying more proxies if successful
            else:
                retries += 1

        if not data_fetched:
            logger.error(f"Failed to fetch data for country {country} after trying all proxies.")


def extract_channel_list(json_data):
    channel_list = []
    json_data = [json_data] if not isinstance(json_data, list) else json_data

    for item in json_data:
        content_ids_by_container = item.get('epg', {}).get('contentIdsByContainer', {})
        for container_list in content_ids_by_container.values():
            for category in container_list:
                channel_list.extend(category.get('contents', []))

    return channel_list


def create_m3u_playlist(epg_data, group_mapping, country):
    # Sort the channels alphabetically by their names
    sorted_epg_data = sorted(epg_data, key=lambda x: x.get('title', '').lower())

    playlist = f"#EXTM3U url-tvg=\"https://github.com/dtankdempse/tubi-m3u/raw/refs/heads/main/tubi_epg_{country}.xml\"\n"
    seen_urls = set()  # Set to track URLs that have already been added

    for elem in sorted_epg_data:
        channel_name = elem.get('title', 'Unknown Channel')

        # Directly decode to UTF-8 to handle special characters properly
        channel_name = channel_name.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')

        stream_url = unquote(elem['video_resources'][0]['manifest']['url']) if elem.get('video_resources') else ''
        clean_url = clean_stream_url(stream_url)  # Clean the URL to remove tokens and other parameters
        tvg_id = str(elem.get('content_id', ''))  # Ensure content ID is treated as a string
        logo_url = elem.get('images', {}).get('thumbnail', [None])[0]  # Extract the logo URL

        # Get the group-title using the group_mapping and fallback to 'Other' if not found
        group_title = group_mapping.get(tvg_id, 'Other')
        group_title = group_title.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')

        # Check if the URL is already in the set of seen URLs to prevent duplicates
        if clean_url and clean_url not in seen_urls:
            playlist += f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo_url}" group-title="{group_title}",{channel_name}\n{clean_url}\n'
            seen_urls.add(clean_url)  # Add the URL to the set of seen URLs

    return playlist


def clean_stream_url(url):
    parsed_url = urlparse(url)
    clean_url = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
    return clean_url


def create_epg_xml(epg_data):
    root = ET.Element("tv")

    for station in epg_data:
        channel = ET.SubElement(root, "channel", id=str(station.get("content_id")))
        display_name = ET.SubElement(channel, "display-name")
        display_name.text = station.get("title", "Unknown Title")

        thumbnails = station.get("images", {}).get("thumbnail", [])
        icon_src = thumbnails[0] if thumbnails else None
        if icon_src:
            ET.SubElement(channel, "icon", src=icon_src)

        for program in station.get('programs', []):
            programme = ET.SubElement(root, "programme", channel=str(station.get("content_id")))

            # Convert start and stop times to XMLTV format
            start_time = convert_to_xmltv_format(program.get("start_time", ""))
            stop_time = convert_to_xmltv_format(program.get("end_time", ""))

            programme.set("start", start_time)
            programme.set("stop", stop_time)

            title = ET.SubElement(programme, "title")
            title.text = program.get("title", "")

            if program.get("description"):
                desc = ET.SubElement(programme, "desc")
                desc.text = program.get("description", "")

    tree = ET.ElementTree(root)
    return tree


def convert_to_xmltv_format(iso_time):
    """Convert ISO 8601 time to XMLTV format."""
    try:
        # Parse the ISO 8601 time (example: 2024-10-08T01:00:00Z)
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ")
        # Format it to XMLTV format (example: 20241008010000 +0000)
        xmltv_time = dt.strftime("%Y%m%d%H%M%S +0000")
        return xmltv_time
    except ValueError:
        # Return the original time if it fails to parse
        return iso_time


if __name__ == "__main__":
    main()
