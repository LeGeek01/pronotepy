import requests
from bs4 import BeautifulSoup
import random
from Crypto.Hash import MD5, SHA256
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.Util import Padding
from Crypto.PublicKey import RSA
import base64
import logging
import datetime
import math
from pronotepy import dataClasses

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


class Client(object):
    """A PRONOTE client with basic functions."""

    def __init__(self, pronote_url):
        log.info('INIT')
        self.communication = Communication(pronote_url)
        self.options, self.func_options = self.communication.initialise()
        self.encryption = Encryption()
        self.encryption.aes_iv = self.communication.encryption.aes_iv
        self.auth_response = self.auth_cookie = self.autorisations = None
        self.date = datetime.datetime.now()
        self.start_day = datetime.datetime.strptime(
            self.func_options.json()['donneesSec']['donnees']['General']['PremierLundi']['V'], '%d/%m/%Y')
        self.week = self._get_week(datetime.date.today())
        self.homepage = None
        hour_start = datetime.datetime.strptime(
            self.func_options.json()['donneesSec']['donnees']['General']['ListeHeures']['V'][0]['L'], '%Hh%M')
        hour_end = datetime.datetime.strptime(
            self.func_options.json()['donneesSec']['donnees']['General']['ListeHeuresFin']['V'][0]['L'], '%Hh%M')
        self.one_hour_duration = hour_end - hour_start

    def login(self, username, password):
        # identification phase
        ident_json = {
            "genreConnexion": 0,
            "genreEspace": int(self.options['a']),
            "identifiant": username,
            "pourENT": False,
            "enConnexionAuto": False,
            "demandeConnexionAuto": False,
            "demandeConnexionAppliMobile": False,
            "demandeConnexionAppliMobileJeton": False,
            "uuidAppliMobile": "",
            "loginTokenSAV": ""}
        idr = self.communication.post("Identification", {'donnees': ident_json})
        log.debug('indentification')

        # creating the authentification data
        id_response = idr.json()
        alea = id_response['donneesSec']['donnees']['alea']
        challenge = id_response['donneesSec']['donnees']['challenge']
        e = Encryption()
        e.aes_set_iv(self.communication.encryption.aes_iv)

        # key gen
        motdepasse = SHA256.new(str(alea + password).encode()).hexdigest().upper()
        e.aes_key = MD5.new((username + motdepasse).encode()).digest()
        del password

        # challenge
        dec = e.aes_decrypt(bytes.fromhex(challenge))
        dec_no_alea = enleverAlea(dec.decode())
        ch = e.aes_encrypt(dec_no_alea.encode()).hex()

        # send
        auth_json = {"connexion": 0, "challenge": ch, "espace": int(self.options['a'])}
        self.auth_response = self.communication.post("Authentification", {'donnees': auth_json})
        if 'cle' in self.auth_response.json()['donneesSec']['donnees']:
            self.communication.after_auth(self.auth_response, e.aes_key)
            self.autorisations = self.auth_response.json()['donneesSec']['donnees']['autorisations']
            log.info(f'successfully logged in as {username}')
            # self.homepage = self._get_homepage_info()
            return True
        else:
            log.info('login failed')
            return False

    def _get_homepage_info(self):
        """Old function, not used"""
        dta_nav = {"_Signature_": {"onglet": 7}, "donnees": {"onglet": 7, "ongletPrec": 7}}
        self.communication.post('Navigation', dta_nav)
        date_str = self.date.strftime('%d/%m/%Y 0:0:0')
        dta = {"_Signature_": {"onglet": 7},
               "donnees": {
                   "avecConseilDeClasse": True,
                   "dateGrille": {"_T": 7, "V": date_str},
                   "numeroSemaine": self.week,
                   "AppelNonFait": {"date": {"_T": 7, "V": date_str}},
                   "CDTNonSaisi": {"numeroSemaine": self.week},
                   "coursNonAssures": {"numeroSemaine": self.week},
                   "personnelsAbsents": {"numeroSemaine": self.week},
                   "incidents": {"numeroSemaine": self.week},
                   "donneesVS": {"numeroSemaine": self.week},
                   "donneesProfs": {"numeroSemaine": self.week},
                   "EDT": {"date": {"_T": 7, "V": date_str}, "numeroSemaine": self.week},
                   "menuDeLaCantine": {"date": {"_T": 7, "V": date_str}},
                   "partenaireCDI": {"CDI": {}}}}
        p_a_response = self.communication.post('PageAccueil', dta)
        log.info('successfully got data')
        return p_a_response.json()

    def _get_week(self, date: datetime.date):
        return int(1 + math.floor((date - self.start_day.date()).days / 7))

    def lessons(self, date_from: datetime.date, date_to: datetime.date = None):
        if not date_to:
            date_to = date_from
        user = self.auth_response.json()['donneesSec']['donnees']['ressource']
        data = {"_Signature_": {"onglet": 16},
                "donnees": {"ressource": user,
                            "numeroSemaine": 0, "avecAbsencesEleve": False, "avecConseilDeClasse": True,
                            "estEDTPermanence": False, "avecAbsencesRessource": True,
                            "avecDisponibilites": True, "avecInfosPrefsGrille": True,
                            "Ressource": user,
                            "NumeroSemaine": 0}}
        output = []
        for i in range(self._get_week(date_from), self._get_week(date_to) + 1):
            data['donnees']['numeroSemaine'] = i
            data['donnees']['NumeroSemaine'] = i
            response = self.communication.post('PageEmploiDuTemps', data)
            l_list = response.json()['donneesSec']['donnees']['ListeCours']
            for lesson in l_list:
                l_object = dataClasses.Lesson(self, lesson)
                if l_object is not None and date_from <= l_object.start.date() <= date_to:
                    output.append(l_object)
        return output


class Communication(object):
    def __init__(self, site):
        """Handles all communication with the PRONOTE servers"""
        self.root_site, self.html_page = self.get_root_address(site)
        self.session = requests.Session()
        self.encryption = Encryption()
        self.attributes = {}
        self.request_number = 1
        self.cookies = None

    def initialise(self):
        """
        Initialisation of the communication. Sets up the encryption and sends the IV for AES to PRONOTE.
        From this point, everything is encrypted with the communicated IV.
        """
        # some headers to be real
        headers = {'connection': 'keep-alive',
                   'cache-control': 'max-age=0',
                   'DNT': '1',
                   'Upgrade-Insecure-Requests': '1',
                   'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                                 'Chrome/79.0.3945.117 Safari/537.36',
                   'Sec-Fetch-User': '?1',
                   'Accept': '*/*',
                   'Sec-Fetch-Site': 'same-origin',
                   'Sec-Fetch-Mode': 'cors',
                   'Accept-Encoding': 'gzip, deflate, br',
                   'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8,cs;q=0.7'}

        # get rsa keys and session id
        get_response = self.session.request('GET', f'{self.root_site}/{self.html_page}', headers=headers)
        self.attributes = self._parse_html(get_response.content)
        # uuid
        self.encryption.rsa_keys = {'MR': self.attributes['MR'], 'ER': self.attributes['ER']}
        uuid = base64.b64encode(self.encryption.rsa_encrypt(self.encryption.aes_iv_temp)).decode()
        # post
        json_post = {'Uuid': uuid}
        initial_response = self.post('FonctionParametres', {'donnees': json_post})
        self.encryption.aes_set_iv()
        return self.attributes, initial_response

    def post(self, function_name: str, data: dict):
        """
        Handler for all POST requests by the api to PRONOTE servers. Automatically provides all needed data for the
        verification of posts. Session id and order numbers are preserved.

        :param function_name:the name of the function (ex.: Authentification)
        :param data:the data that will be sent in the donneesSec dictionary.
        """
        if type(data) != dict:
            return PronoteAPIError('POST error: donnees not dict')

        r_number = self.encryption.aes_encrypt(str(self.request_number).encode()).hex()
        json = {'session': int(self.attributes['h']), 'numeroOrdre': r_number, 'nom': function_name,
                'donneesSec': data}
        p_site = self.root_site + '/appelfonction/' + self.attributes['a'] + '/' + self.attributes['h'] + '/' + r_number
        response = self.session.request('POST', p_site, json=json, cookies=self.cookies)
        self.request_number += 2

        # error protection
        # TODO: false positive bad numero ordre, make better error handler
        if 'Erreur' in response.json():
            log.error(f'POST ERROR {response.json()["Erreur"]["G"]}')
            log.error(response.content)
            raise PronoteAPIError(f'POST error: got error {response.json()["Erreur"]["G"]}')
        # elif self.encryption.aes_encrypt(str(self.request_number - 1).encode()).hex().upper() != response.json()['numeroOrdre']:
        #     log.warning(f'bad numeroOrdre: {response.json()["numeroOrdre"]}')
        return response

    def after_auth(self, auth_response, auth_key):
        """
        Key change after the authentification was successful.
        :param auth_response:the authentification response from the server
        :param auth_key:authentification key used to calculate the challenge. (from password of the user)
        """
        self.encryption.aes_key = auth_key
        self.cookies = auth_response.cookies
        work = self.encryption.aes_decrypt(bytes.fromhex(auth_response.json()['donneesSec']['donnees']['cle']))
        # ok
        key = MD5.new(enBytes(work.decode()))
        key = key.digest()
        self.encryption.aes_key = key

    @staticmethod
    def _parse_html(html):
        """Parses the html for the RSA keys"""
        parsed = BeautifulSoup(html, "html.parser")
        onload = parsed.find(id='id_body')
        if onload:
            onload_c = onload['onload'][14:-37]
        else:
            raise PronoteAPIError("The html parser couldn't find the json data.")
        attributes = {}
        for attr in onload_c.split(','):
            key, value = attr.split(':')
            attributes[key] = value.replace("'", '')
        return attributes

    @staticmethod
    def get_root_address(addr):
        return '/'.join(addr.split('/')[:-1]), '/'.join(addr.split('/')[-1:])


class ClientStudent(Client):
    """
    PRONOTE client for student accounts.
    """
    def __init__(self, pronote_url):
        super(ClientStudent, self).__init__(pronote_url)
        self.periods_ = self.periods()

    def periods(self):
        if hasattr(self, 'periods_'):
            return self.periods_
        json = self.func_options.json()['donneesSec']['donnees']['General']['ListePeriodes']
        return [dataClasses.Period(self, j) for j in json]

    def current_periods(self):
        output = []
        for p in self.periods_:
            if p.start < self.date < p.end:
                output.append(p)
        return output

    def homework(self, date_from: datetime.date, date_to: datetime.date = None):
        if not date_to:
            date_to = datetime.datetime.strptime(
                self.func_options.json()['donneesSec']['donnees']['General']['DerniereDate']['V'], '%d/%m/%Y').date()
        json_data = {'donnees': {
            'domaine': {'_T': 8, 'V': f"[{self._get_week(date_from)}..{self._get_week(date_to)}]"}},
            '_Signature_': {'onglet': 88}}
        response = self.communication.post('PageCahierDeTexte', json_data)
        h_list = response.json()['donneesSec']['donnees']['ListeTravauxAFaire']['V']
        return [dataClasses.Homework(self, h) for h in h_list]


class ClientTeacher(Client):
    def __init__(self, pronote_url):
        super(ClientTeacher, self).__init__(pronote_url)


def create_random_string(length):
    output = ''
    for _ in range(length + 1):
        j = random.choice('ABCDEFGHIJKLMNOPQRSTUVabcdefghijklmnopqrstuv123456789')
        output += j
    return output


def enleverAlea(text):
    """Gets rid of the stupid thing that they did, idk what it really is for, but i guess it adds security"""
    sansalea = []
    for i, b in enumerate(text):
        if i % 2 == 0:
            sansalea.append(b)
    return ''.join(sansalea)


def enBytes(string: str):
    list_string = string.split(',')
    return bytes([int(i) for i in list_string])


class Encryption(object):
    def __init__(self):
        """The encryption part of the API. You shouldn't have to use this normally."""
        # aes
        self.aes_iv = bytes(16)
        self.aes_iv_temp = create_random_string(15).encode()
        self.aes_key = MD5.new().digest()
        # rsa
        self.rsa_keys = {}

    def aes_encrypt(self, data: bytes):
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_iv)
        padded = Padding.pad(data, 16)
        return cipher.encrypt(padded)

    def aes_decrypt(self, data: bytes):
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_iv)
        return Padding.unpad(cipher.decrypt(data), block_size=16)

    def aes_set_iv(self, iv=None):
        if iv is None:
            self.aes_iv = MD5.new(self.aes_iv_temp).digest()
        else:
            self.aes_iv = iv

    def rsa_encrypt(self, data: bytes):
        key = RSA.construct((int(self.rsa_keys['MR'], 16), int(self.rsa_keys['ER'], 16)))
        # noinspection PyTypeChecker
        pkcs = PKCS1_v1_5.new(key)
        return pkcs.encrypt(data)


class PronoteAPIError(Exception):
    pass
