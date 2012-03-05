#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2011 Deepin, Inc.
#               2011 Hou Shaohui
#
# Author:     Hou Shaohui <houshao55@gmail.com>
# Maintainer: Hou ShaoHui <houshao55@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import gobject
from time import time, sleep
import gst
import gst.interfaces
import sys

from config import config
from library import MediaDB
from logger import Logger
from player.fadebin import PlayerBin
from utils import get_mime_type, get_uris_from_pls, get_uris_from_m3u
gobject.threads_init()

DEBUG = False

class DeepinMusicPlayer(gobject.GObject, Logger):
    __gsignals__ = {
        "play-end" : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        "new-song" : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, (gobject.TYPE_PYOBJECT,)),
        "instant-new-song" : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, (gobject.TYPE_PYOBJECT,)),
        "paused"   : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        "played"   : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        "stopped"  : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        "seeked"   : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ())
        }
    
    def __init__(self):
        gobject.GObject.__init__(self)
        
        # Init.
        self.song = None        # current song
        self.__source = None  # playlist
        self.__need_load_prefs = True # reload
        self.__current_stream_seeked = False # can be seeked
        self.__next_already_called = False
        self.__emit_signal_new_song_id = None
        
        self.stop_after_this_track = False
        self.__current_song_reported = False
        self.__current_duration = None
        
        MediaDB.connect("simple-changed", self.__on_change_songs)
        
        self.bin = PlayerBin()
        self.bin.connect("eos",   self.__on_eos)
        self.bin.connect("error", self.__on_error)
        self.bin.connect("tags-found", self.__on_tag)
        self.bin.connect("tick", self.__on_tick)
        self.bin.connect("playing-stream", self.__on_playing)
        
    def __on_change_songs(self, db, songs):    
        if self.song in songs:
            self.song = songs[songs.index(self.song)]
        
    def __on_eos(self, bin, uri):    
        self.loginfo("received eos for %s", uri)
        if uri == self.song.get("uri") and not self.__next_already_called:        
            self.loginfo("request new song: eos and play-end not emit")
            self.emit("play-end")
            self.next(gapless=True, auto=True) # todo
        self.__next_already_called = False    
        
    def __on_error(self, bin, uri):   
        self.logdebug("gst error received for %s", uri)
        self.bin.xfade_close()
        config.set("player", "play", "false")
        self.emit("paused")
        
        if uri == self.song.get("uri") and not self.__next_already_called:
            self.loginfo("request new song: error and play-end not emit")
            self.emit("play-end")
            self.next(gapless=True, auto=True)
        self.__next_already_called = False    
        
    def __on_tag(self, bin, taglist):    
        ''' The playbin found the tag information'''
        if self.song and not self.song.get("title"):
            self.loginfo("tag found %s", taglist)
            IDS = {
                "title": "artist",
                #"genre": "genre",
                #"artist": "artist",
                #"album": "album",
                #"bitrate": "#bitrate",
                #'track-number':"#track"
            }
            mod = {}
            for key in taglist.keys():
                if IDS.has_key(key):
                    if key == "lenght":
                        value = int(taglist[key]) * 1000
                    elif key == "bitrate":    
                        value = int(taglist[key] / 100)
                    elif isinstance(taglist[key], long):
                        value = int(taglist[key])
                    else:    
                        value = taglist[key]
                    mod[IDS[key]] = value    
            MediaDB.set_property(self.song, mod)        
            
    def __on_tick(self, bin, pos, duration):        
        if not duration or duration <= 0:
            return
        else:
            if not self.song.get("#duration"):
                MediaDB.set_property(self.song, {"#duration": duration * 1000})
                
        self.perhap_report(pos, duration)        # todo
        crossfade = self.get_crossfade()
        if crossfade < 0:
            crossfade = 0
        remaining = duration - pos    
        if crossfade:
            if remaining < crossfade:
                if not self.__next_already_called and remaining > 0:
                    self.loginfo("request new song: on tick and play-end not emit")
                    self.next(gapless=True, auto=True)
                    self.emit("play-end")
                    self.__next_already_called = True
            else:        
                self.__next_already_called = False
        else:        
            self.__next_already_called = False
            
    def __on_playing(self, bin, uri):
        '''Signal emitted by fadebin when a new stream previously queued start'''
        if uri == self.song.get("uri"):
            self.loginfo("signal playing-stream receive by %s", uri)
            config.set("player", "play", "true")
            self.emit("played")
            self.__emit_signal_new_song()
            
    def __emit_signal_new_song(self):        
        def real_emit_signal_new_song():
            self.emit("new-song", self.song)
            self.__emit_signal_new_song_id = None
            
        if self.__emit_signal_new_song_id is not None:    
            gobject.source_remove(self.__emit_signal_new_song_id)
            self.__emit_signal_new_song_id = None
        self.__emit_signal_new_song_id = gobject.timeout_add(5000, real_emit_signal_new_song)    
    
                    
    def set_source(self, source):    
        self.__source = source
        
    def set_song(self, song, play=False, crossfade=None, seek=None):
        '''set song'''
        # report playcount
        self.perhap_report()
        
        self.stop_after_this_track = False
        self.__need_load_prefs = False
        
        if seek:
            self.__current_stream_seeked = True
        else:    
            self.__current_stream_seeked = False
            
        # get crossfade.    
        if crossfade is None:    
            crossfade = self.get_crossfade()
        
        uri = song.get('uri')    
        self.loginfo("player try to load %s", uri)
        
        # if has xiami , then stop xiami of the previous song.
        
        mime_type = get_mime_type(uri)

        if mime_type in [ "audio/x-scpls", "audio/x-mpegurl"]:
            try_num = 2
            uris = None
            while not uris:
                if mime_type == "audio/x-scpls":
                    uris = get_uris_from_pls(uri)
                elif mime_type == "audio/x-mpegurl":    
                    uris = get_uris_from_m3u(uri)
                try_num += 1    
                if try_num > 3:
                    break
            if uris:        
                self.loginfo("%s choosen in %s", uris[0], uri)
                uri = uris[0]
            else:    
                self.loginfo("no playable uri found in %s", uri)
                uri = None
                
        # remove old stream for pipeline excepted when need to fade
        if self.song and (crossfade == -1 or self.is_paused() or not self.is_playable()):        
            self.logdebug("force remove stream:%s", self.song.get("uri"))
            self.bin.xfade_close(self.song_get("uri"))
            
        # set current song and try play it.
        self.song = song    
        self.__current_song_reported = False
        self.emit("instant-new-song", self.song)
        ret = uri and self.bin.xfade_open(uri)
        if not ret:
            gobject.idle_add(self.emit, "play-end")
            self.next(auto=True)
        elif play:    
            self.play(crossfade, seek)
                
        
    def play_new(self, song, crossfade=None, seek=None):
        '''add new song and try to play it'''
        self.set_song(song, True, crossfade, seek)
        
    def play(self, crossfade=-1, seek=None):    
        '''play currnet song'''
        if seek:
            crossfade = -1
        ret = self.bin.xfade_play(crossfade)    
        if not ret:
            self.emit("paused")
            config.set("player", "play", "false")
            gobject.idle_add(self.emit, "play-end")
        else:    
            if seek:
                self.seek(seek)
            self.emit("played")    
        return ret    
    
    def pause(self):
        '''pause'''
        self.bin.xfade_pause()
        config.set("player", "play", "false")
        self.emit("paused")
        
    def stop(self):    
        self.stop_after_this_track = False
        self.update_skipcount()
        
        # whether is xiami, try to stop it.
        # TODO
        
        # stop local player
        self.bin.xfade_close()
        config.set("player", "play", "false")
        self.emit("stopped")
        
    def previous(self):    
        '''previous song'''
        self.update_skipcount()
        if self.__source:
            song = self.__source.get_previous_song()
            if song:
                self.play_new(song)
                return
        self.stop()    
        
    def next(self, gapless=False, auto=False):    
        '''next song'''
        self.update_skipcount()
        if not self.__source:
            return
        
        if self.stop_after_this_track and auto:
            self.loginfo("stop after this track request")
            self.fadeout_and_stop()
        else:    
            data = self.__source.get_next_song()
            if data:
                song, stop_after_this_track = data        
                if gapless and config.getboolean("player", "crossfade") and config.getboolean("player", "crossfade_gapless_album") and self.song and song.get("album") == self.song.get("album"):        
                    self.logdebug("request gapless to the backend")
                    self.play_new(song, 0)
                else:    
                    self.play_new(song)
                    
                self.stop_after_this_track = stop_after_this_track    
                return 
            else:
                # stop the current song
                self.fadeout_and_stop()
                
    def rewind(self):            
        '''rewind'''
        length = self.get_length()
        if not length:
            self.loginfo("Can't rewind a stream with on duration")
            return
        jump = max(5, length * 0.05)
        pos = self.get_position()
        if pos >= 0:
            pos = max(0, pos - jump)
            self.seek(pos)
            
    def forward(self):        
        '''forward'''
        length = self.get_length()
        if not length:
            self.loginfo("Can't forward a stream with on duration")
            return
        jump = max(5, length * 0.05)
        pos = float(self.get_position())
        if pos >=0:
            pos - float(min(pos+jump, length))
            self.logdebug("request seek to %d", pos)
            self.seek(pos)
            
    def playpause(self):        
        '''play or pause'''
        self.logdebug("is paused %s ?", self.is_paused())
        if not self.is_paused():
            self.pause()
        else:    
            self.logdebug('is playable ? %s', self.is_playable())
            if self.is_playable():
                self.play(-1)
            else:    
                self.logdebug("have song %s", self.song)
                if self.song:
                    # Reload the current song
                    self.play_new(self.song)
                else:    
                    self.stop()
                    
    def seek(self, pos):
        '''seek'''
        #xiami
        
        if self.bin.xfade_seekable():
            self.__current_stream_seeked = True
            pos = max(0, pos)
            self.bin.xfade_set_time(pos)
            self.emit("seeked")
        else:                                
            self.logdebug("current song is not seekable")
            
    def set_volume(self, num):                    
        self.__volume = num
        self.bin.set_volume(num)
        
    def get_volume(self):    
        return self.bin.get_volume()
    volume = property(get_volume, set_volume)
        
    def is_paused(self):                
        '''whether the current song is paused.'''
        return not self.bin.xfade_playing()
    
    def is_playable(self):
        '''whethe the current stream is opened'''
        return self.bin.xfade_opened()
    
    def get_position(self):
        '''get postion'''
        pos = self.bin.xfade_get_time()
        # todo xiami
        
        # return value
        return int(pos)
    
    def get_length(self):
        '''get lenght'''
        if self.song is not None:
            duration = self.bin.xfade_get_duration()
            if duration != -1:
                # if current song_dict not have '#duration' and set it
                if not self.song.get("#duration"):
                    MediaDB.set_property(self.song, {"#duration": duration * 1000})
                return duration    
            elif self.song.get("#duration"):
                return self.song.get("#duration") / 1000
        return 0    
    
    def get_crossfade(self):
        '''get crossfade'''
        if config.getboolean("player", "crossfade"):
            try:
                crossfade = float(config.get("player", "crossfade_time"))
            except:    
                crossfade = 3.5
            if crossfade > 50:    
                crossfade = 3.5
        else:        
            crossfade = -1
        return crossfade    
    
    def perhap_report(self, pos=None, duration=None):
        '''report song'''
        if not duration:
            duration = self.get_length()
        if not pos:    
            pos = self.get_position()
        if self.song and not self.__current_stream_seeked and not self.__next_already_called and not self.__current_song_reported and duration > 10 and pos and pos >= min(duration / 2, 240 * 1000):    
            MediaDB.set_property(self.song, {"#playcount": self.song.get("#playcount", 0) + 1})
            MediaDB.set_property(self.song, {"#lastplayed":time()})
            self.__current_song_reported = True
            # stamp = str(int(time()) - duration)
    
    def current_stream_seeked(self):        
        return self.__current_stream_seeked
    
    def load(self):
        '''load configure'''
        if not self.__need_load_prefs:
            return
        
        # get uri
        uri = config.get("player", "uri")
        
        # get seek
        seek = int(config.get("player", "seek"))
        
        # get state 
        state = config.get("player", "state")
        
        # Init player state
        play = False
        self.loginfo("player load %s in state %s at %d", uri, state, seek)
        if config.getboolean("player", "play_on_startup") and state == "playing":
            play = True
            
        # load uri
        if uri:    
            song = MediaDB.get_song(uri)
            if song:
                self.set_song(song, play, self.get_crossfade() * 2, seek / 1000)
        
    def save_state(self):            
        '''save current song's state'''
        if self.song:
            config.set("player", "uri", self.song.get("uri"))
            config.set("player", "seek", str(self.get_position()))
            if not self.is_playable():
                state = "stop"
            elif self.is_paused():    
                state = "paused"
            else:    
                state = "playing"
            config.set("player", "state", state)    
            self.loginfo("player status saved, %s", state)
                

Player = DeepinMusicPlayer()            
