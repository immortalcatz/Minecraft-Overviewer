#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

import multiprocessing
import itertools
from itertools import cycle, islice
import os
import os.path
import functools
import re
import shutil
import collections
import json
import logging
import util
import cPickle
import stat
import errno 
import time
from time import gmtime, strftime, sleep


"""
This module has routines related to distributing the render job to multipule nodes

"""

def catch_keyboardinterrupt(func):
    """Decorator that catches a keyboardinterrupt and raises a real exception
    so that multiprocessing will propagate it properly"""
    @functools.wraps(func)
    def newfunc(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            logging.error("Ctrl-C caught!")
            raise Exception("Exiting")
        except:
            import traceback
            traceback.print_exc()
            raise
    return newfunc
    
child_rendernode = None
def pool_initializer(rendernode):
    logging.debug("Child process {0}".format(os.getpid()))
    #stash the quadtree objects in a global variable after fork() for windows compat.
    global child_rendernode
    child_rendernode = rendernode  
    
#http://docs.python.org/library/itertools.html    
def roundrobin(iterables):
    "roundrobin('ABC', 'D', 'EF') --> A D E B F C"
    # Recipe credited to George Sakkis
    pending = len(iterables)
    nexts = cycle(iter(it).next for it in iterables)
    while pending:
        try:
            for next in nexts:
                yield next()
        except StopIteration:
            pending -= 1
            nexts = cycle(islice(nexts, pending))

            
class RenderNode(object):
    def __init__(self, world, quadtrees):
        """Distributes the rendering of a list of quadtrees. All of the quadtrees must have the same world."""

        if not len(quadtrees) > 0:
            raise ValueError("there must be at least one quadtree to work on")    

        self.world = world
        self.quadtrees = quadtrees
        #bind an index value to the quadtree so we can find it again
        i = 0
        for q in quadtrees:
            q._render_index = i
            i += 1       

    def print_statusline(self, complete, total, level, unconditional=False):
        if unconditional:
            pass
        elif complete < 100:
            if not complete % 25 == 0:
                return
        elif complete < 1000:
            if not complete % 100 == 0:
                return
        else:
            if not complete % 1000 == 0:
                return
        logging.info("{0}/{1} tiles complete on level {2}/{3}".format(
                complete, total, level, self.max_p))
                
    def go(self, procs):
        """Renders all tiles"""
        
        logging.debug("Parent process {0}".format(os.getpid()))
        # Create a pool
        if procs == 1:
            pool = FakePool()
            global child_rendernode
            child_rendernode = self
        else:
            pool = multiprocessing.Pool(processes=procs,initializer=pool_initializer,initargs=(self,))
            #warm up the pool so it reports all the worker id's
            pool.map(bool,xrange(multiprocessing.cpu_count()),1)        
                
        quadtrees = self.quadtrees
        
        # do per-quadtree init.
        max_p = 0        
        total = 0
        for q in quadtrees:
            total += 4**q.p
            if q.p > max_p:
                max_p = q.p
            q.go(procs) 
        self.max_p = max_p
        # Render the highest level of tiles from the chunks
        results = collections.deque()
        complete = 0
        logging.info("Rendering highest zoom level of tiles now.")
        logging.info("Rendering {0} layer{1}".format(len(quadtrees),'s' if len(quadtrees) > 1 else '' ))
        logging.info("There are {0} tiles to render".format(total))        
        logging.info("There are {0} total levels to render".format(self.max_p))
        logging.info("Don't worry, each level has only 25% as many tiles as the last.")
        logging.info("The others will go faster")
        count = 0
        batch_size = 4*len(quadtrees)
        while batch_size < 10:
            batch_size *= 2
        timestamp = time.time()
        for result in self._apply_render_worldtiles(pool,batch_size):
            results.append(result)               
            # every second drain some of the queue
            timestamp2 = time.time()
            if timestamp2 >= timestamp + 1:
                timestamp = timestamp2                
                count_to_remove = (1000//batch_size)
                if count_to_remove < len(results):
                    while count_to_remove > 0:
                        count_to_remove -= 1
                        complete += results.popleft().get()
                        self.print_statusline(complete, total, 1)  
            if len(results) > (10000//batch_size):
                # Empty the queue before adding any more, so that memory
                # required has an upper bound
                while len(results) > (500//batch_size):
                    complete += results.popleft().get()
                    self.print_statusline(complete, total, 1)

        # Wait for the rest of the results
        while len(results) > 0:
            complete += results.popleft().get()
            self.print_statusline(complete, total, 1)

        self.print_statusline(complete, total, 1, True)

        # Now do the other layers
        for zoom in xrange(self.max_p-1, 0, -1):
            level = self.max_p - zoom + 1
            assert len(results) == 0
            complete = 0
            total = 0
            for q in quadtrees:
                if zoom <= q.p:
                    total += 4**zoom
            logging.info("Starting level {0}".format(level))
            timestamp = time.time()
            for result in self._apply_render_inntertile(pool, zoom,batch_size):
                results.append(result)
                # every second drain some of the queue
                timestamp2 = time.time()
                if timestamp2 >= timestamp + 1:
                    timestamp = timestamp2                
                    count_to_remove = (1000//batch_size)
                    if count_to_remove < len(results):
                        while count_to_remove > 0:
                            count_to_remove -= 1
                            complete += results.popleft().get()
                            self.print_statusline(complete, total, 1)    
                if len(results) > (10000/batch_size):
                    while len(results) > (500/batch_size):
                        complete += results.popleft().get()
                        self.print_statusline(complete, total, level)
            # Empty the queue
            while len(results) > 0:
                complete += results.popleft().get()
                self.print_statusline(complete, total, level)

            self.print_statusline(complete, total, level, True)

            logging.info("Done")

        pool.close()
        pool.join()

        # Do the final one right here:
        for q in quadtrees:
            q.render_innertile(os.path.join(q.destdir, q.tiledir), "base")
        
        
    def _get_chunks_in_range(self, colstart, colend, rowstart, rowend):
        """Get chunks that are relevant to the tile rendering function that's
        rendering that range"""
        chunklist = []
        unconvert_coords = self.world.unconvert_coords
        #get_region_path = self.world.get_region_path
        get_region = self.world.regionfiles.get
        for row in xrange(rowstart-16, rowend+1):
            for col in xrange(colstart, colend+1):
                # due to how chunks are arranged, we can only allow
                # even row, even column or odd row, odd column
                # otherwise, you end up with duplicates!
                if row % 2 != col % 2:
                    continue
                
                # return (col, row, chunkx, chunky, regionpath)
                chunkx, chunky = unconvert_coords(col, row)
                #c = get_region_path(chunkx, chunky)
                _, _, c, mcr = get_region((chunkx//32, chunky//32),(None,None,None,None));
                if c is not None and mcr.chunkExists(chunkx,chunky):                  
                    chunklist.append((col, row, chunkx, chunky, c))
        return chunklist        
        

        
    def _apply_render_worldtiles(self, pool,batch_size):
        """Returns an iterator over result objects. Each time a new result is
        requested, a new task is added to the pool and a result returned.
        """
        if batch_size < len(self.quadtrees):
            batch_size = len(self.quadtrees)         
        batch = []
        jobcount = 0       
        # roundrobin add tiles to a batch job (thus they should all roughly work on similar chunks)
        iterables = [q.get_worldtiles() for q in self.quadtrees]
        for job in roundrobin(iterables):
            # fixup so the worker knows which quadtree this is                 
            job[0] = job[0]._render_index      
            # Put this in the batch to be submited to the pool  
            batch.append(job)
            jobcount += 1
            if jobcount >= batch_size:
                jobcount = 0        
                yield pool.apply_async(func=render_worldtile_batch, args= [batch])
                batch = []              
        if jobcount > 0:
            yield pool.apply_async(func=render_worldtile_batch, args= [batch])         

    def _apply_render_inntertile(self, pool, zoom,batch_size):
        """Same as _apply_render_worltiles but for the inntertile routine.
        Returns an iterator that yields result objects from tasks that have
        been applied to the pool.
        """
        
        if batch_size < len(self.quadtrees):
            batch_size = len(self.quadtrees)
        batch = []
        jobcount = 0
        # roundrobin add tiles to a batch job (thus they should all roughly work on similar chunks)
        iterables = [q.get_innertiles(zoom) for q in self.quadtrees if zoom <= q.p]
        for job in roundrobin(iterables):
            # fixup so the worker knows which quadtree this is  
            job[0] = job[0]._render_index
            # Put this in the batch to be submited to the pool  
            batch.append(job)
            jobcount += 1
            if jobcount >= batch_size:
                jobcount = 0
                yield pool.apply_async(func=render_innertile_batch, args= [batch])
                batch = []
                
        if jobcount > 0:
            yield pool.apply_async(func=render_innertile_batch, args= [batch])    
            
@catch_keyboardinterrupt
def render_worldtile_batch(batch):   
    global child_rendernode
    rendernode = child_rendernode
    count = 0
    _get_chunks_in_range = rendernode._get_chunks_in_range
    #logging.debug("{0} working on batch of size {1}".format(os.getpid(),len(batch)))        
    for job in batch:
        count += 1    
        quadtree = rendernode.quadtrees[job[0]]
        colstart = job[1]
        colend = job[2]
        rowstart = job[3]
        rowend = job[4]
        path = job[5]
        path = quadtree.full_tiledir+os.sep+path        
        # (even if tilechunks is empty, render_worldtile will delete
        # existing images if appropriate)    
        # And uses these chunks
        tilechunks = _get_chunks_in_range(colstart, colend, rowstart,rowend)
        #logging.debug(" tilechunks: %r", tilechunks)
        
        quadtree.render_worldtile(tilechunks,colstart, colend, rowstart, rowend, path)      
    return count

@catch_keyboardinterrupt
def render_innertile_batch(batch):    
    global child_rendernode
    rendernode = child_rendernode
    count = 0   
    #logging.debug("{0} working on batch of size {1}".format(os.getpid(),len(batch)))
    for job in batch:
        count += 1        
        quadtree = rendernode.quadtrees[job[0]]               
        dest = quadtree.full_tiledir+os.sep+job[1]
        quadtree.render_innertile(dest=dest,name=job[2])
    return count
    
class FakeResult(object):
    def __init__(self, res):
        self.res = res
    def get(self):
        return self.res
class FakePool(object):
    """A fake pool used to render things in sync. Implements a subset of
    multiprocessing.Pool"""
    def apply_async(self, func, args=(), kwargs=None):
        if not kwargs:
            kwargs = {}
        result = func(*args, **kwargs)
        return FakeResult(result)
    def close(self):
        pass
    def join(self):
        pass
    