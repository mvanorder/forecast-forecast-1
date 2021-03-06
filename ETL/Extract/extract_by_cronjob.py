import os
import json
import time
from urllib.parse import quote

from pyowm import OWM
from pyowm.weatherapi25.forecast import Forecast
from pyowm.exceptions.api_response_error import NotFoundError
from pyowm.exceptions.api_call_error import APICallTimeoutError, APIInvalidSSLCertificateError

from pymongo import MongoClient
from pymongo.collection import Collection, ReturnDocument
from pymongo.errors import ConnectionFailure, InvalidDocument, DuplicateKeyError, OperationFailure

from config import OWM_API_key as key, port, host, user, password, socket_path


API_key = key
owm = OWM(API_key) # the OWM object
print(type(owm))

password = quote(password)    # url encode the password for the mongodb uri
uri = "mongodb+srv://%s:%s@%s" % (user, password, socket_path)
print(uri)

   
def get_data_from_weather_api(owm, request_type, zipcode=None, coords=None):
    ''' Handle the API call errors for weatehr and forecast type calls.

    :param owm: the OWM API object
    :type owm: pyowm.OWM
    :param request_type: should be either 'forecast' or 'weather' to direct the API calls
    :type request_type: string
    :param zipcode: the zipcode reference for the API call
    :type zipcode: string
    :param coords: the latitude and longitude coordinates reference for the API call
    :type coords: 2-tuple

    returns: the API data
    '''

    result = None
    tries = 1
    while result is None and tries <= 3:
        try:
            if coords:
                result = owm.three_hours_forecast_at_coords(*coords)
            elif zipcode:
                result = owm.weather_at_zip_code(zipcode, 'us')
        except APIInvalidSSLCertificateError:
            loc = zipcode or 'lat: {}, lon: {}'.format(str(coords[0]), str(coords[1]))
            print(f'SSL error with {loc} on attempt {tries} ...trying again')
            # ssl_err(owm, which)
        except APICallTimeoutError:
            loc = zipcode or 'lat: {}, lon: {}'.format(str(coords[0]), str(coords[1]))
            print(f'Timeout error with {loc} on attempt {tries}... trying again')
            # timeout_err(owm, which)
        tries += 1
    return result

def read_list_from_file(filename):
    """ Read the zip codes list from the csv file.
        
    :param filename: the name of the file
    :type filename: sting
    """
    with open(filename, "r") as z_list:
        return z_list.read().strip().split(',')

def set_location_and_get_current(code):
    ''' Get the current weather for the given zipcode and create the weather object that will be used in the instant object
    and set the latitude and longitude corrosponding to the zip code.

    :param code: the zip code to find weather data about
    :type code: string

    :return: the modified weather object
    :type: json
    '''
    global owm
    
    obs = get_data_from_weather_api(owm, 'weather', zipcode=str(code))
    # transform the data into the weather object needed in the database
    current = json.loads(obs.to_JSON()) # the current weather for the given zipcode
    # update the 'current' object with the fields needed for making the processing data
    current['instant'] = 10800*(current['Weather']['reference_time']//10800 + 1)
    current['location'] = current['Location']['coordinates']
    current['zipcode'] = code
    current.pop('Location')
    current['Weather'].pop('sunset_time')
    current['Weather'].pop('sunrise_time')
    current['Weather']['temperature'].pop('temp_kf')
    current['Weather'].pop('weather_icon_name')
    current['Weather'].pop('visibility_distance')
    current['Weather'].pop('dewpoint')
    current['Weather'].pop('humidex')
    current['Weather'].pop('heat_index')
    return(current)


def five_day(zlat, zlon):
    ''' Get each weather forecast for the corrosponding zip code.
    
    :return five_day: the five day, every three hours, forecast for the zip code
    :type five_day: dict
    '''
    global owm

    forecaster = get_data_from_weather_api(owm, 'forecast', coords=(zlat, zlon))
    # Get the actual forecasts and add them to a list to be passed on while changing the key 'reference_time' to 'instant'
    forecast = forecaster.get_forecast()
    f = json.loads(forecast.to_JSON())
    forecasts = f['weathers']
    for forecast in forecasts:
        # update the 'current' object with the fields needed for making the processing data
        forecast['instant'] = forecast.pop('reference_time')
        forecast['location'] = f['Location']['coordinates']
        forecast['time_to_instant'] = forecast['instant'] - f['reception_time']
        # remove all the fields that will give null values
        ### You should figure out how to handle the null values ###
        forecast.pop('sunset_time')
        forecast.pop('sunrise_time')
        forecast['temperature'].pop('temp_kf')
        forecast.pop('weather_icon_name')
        forecast.pop('visibility_distance')
        forecast.pop('dewpoint')
        forecast.pop('humidex')
        forecast.pop('heat_index')
    return(forecasts)

def sort_casts(forecasts, code, client):
    ''' Take the array of forecasts from the five day forecast and sort them into the documents of the instants collection.
        
    :param forecasts: the forecasts from five_day()-  They come in a list of 40, one for each of every thrid hour over five days
    :type forecasts: list- expecting a list of forecasts
    :param code: the zipcode
    :type code: string
    :param client: the mongodb client
    :type client: MongoClient
    '''
    global host
    global port

    client = MongoClient(host=host, port=port)
    db = client.OWM
    col = db.instant
    # update each forecast and insert it to the instant document with the matching instant_time and zipcode
    for forecast in forecasts:
        # now find the document that has that code and that ref_time
        # This should find a single instant specified by zip and the forecast ref_time and append the forecast to the forecasts object
        filter_by_zip_and_inst = {'zipcode': code, 'instant': forecast['instant']}
        filters = filter_by_zip_and_inst
        add_forecast_to_instant = {'$push': {'forecasts': forecast}} # append the forecast object to the forecasts list
        updates = add_forecast_to_instant
        updated = col.find_one_and_update(filters, updates, upsert=True, return_document=ReturnDocument.AFTER)

        
def load(data, client, name):
    ''' Add the observed weather to the corrosponding instant document and load it to the remote database 
        
    :param data: the dictionary created from the api calls
    :type data: dict
    :param client: the pymongo client object
    :type client: MongoClient
    :param name: the database collection to be used
    :type name: str
    '''
    
    global uri

    # connect to the local database and search for the matching instant document and update it
    client =  MongoClient(host=host, port=port)
    database = client.OWM
    col = Collection(database, name)
    filters = {'zipcode':data['zipcode'], 'instant':data['instant']}
    updates = {'$set': data['Weather']} # use only the weather object from the current weather created from the API call
    try:
        # check to see if there is a document that fits the parameters. If there is, update it, if there isn't, upsert it
        update = col.find_one_and_update(filters, updates,  upsert=True, return_document=ReturnDocument.AFTER)
        # switch tho the remote database and load the new document to it
        client = MongoClient(uri)
        database =  client.OWM
        col = Collection(database, name)
        loaded = col.insert_one(update)
    except DuplicateKeyError:
        return(f'DuplicateKeyError, could not insert data into {name}.')
    

if __name__ == '__main__':
    try:
        directory = os.path.join(os.environ['HOME'], 'data', 'forcast-forcast')
        filename = os.path.join(directory, 'ETL', 'Extract', 'resources', 'success_zipsNC.csv')
        codes = read_list_from_file(filename)
    except FileNotFoundError:
        print('caught filenotfounderror, trying forcast-forcast')
        directory = os.path.join(os.environ['HOME'], 'data', 'forecast-forecast')
        filename = os.path.join(directory, 'ETL', 'Extract', 'resources', 'success_zipsNC.csv')
        codes = read_list_from_file(filename)
        print('Got it')
    codes = read_list_from_file(filename)
    num_zips = len(codes)
    i, n = 0, 0
    print(f'task began at {time.localtime()}')
    client = MongoClient(uri)
    for code in codes:
        print(f'processing th {n}th')
        current = set_location_and_get_current(code)
        zlat = current['location']['lat']
        zlon = current['location']['lon']
        forecasts = five_day(zlat, zlon)
        sort_casts(forecasts, code, client)
        load(current, client, 'instant')
        n+=1
    client.close()
    print(f'task ended at {time.localtime()} and processed like {n} zipcodes')
