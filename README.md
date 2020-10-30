# CMA pool simulator

This script tries to simulate the algorithm of the [CMA pool branch](https://github.com/bpeel/linux/tree/nroberts/cma-pool-compact) for the Raspberry Pi v3d kernel driver. The idea of the branch is to have a limited pool of CMA memory and copy buffer to and from the pool when needed instead of each buffer allocating its own chunk of CMA memory.

The simulator works by reading a dmesg [log](log.txt) file generated with a [special branch](https://github.com/bpeel/linux/tree/nroberts/bo-usage-logging) of the kernel driver that tracks all of the buffer actions. The script can read this log on the stdin and simulate what the driver would do. In the end it generates a video using PyCairo and ffmpeg. The resulting video can be seen on [YouTube](https://youtu.be/QZngqi833hg). The log file was taken on the Pi running LibreOffice, Chrome and Minecraft simultaneously.

The video shows two different version of the pool. The top one is the basic one and the bottom one adds a tweak to try and proactively compact the pool to reduce the fragmentation. To achieve this, whenever a buffer loses its last usecnt, the driver checks whether there is any free space just behind the buffer and if so it will move the buffer to fit snugly next to its lower neighbour.

In the pool, there are three colours as shown in the legend:

* **Unmoveable**. The buffer is used internally by the kernel and can not be moved or paged out of the pool. Currently this is the large buffer used for binning and the fbcon buffer.
* **In-use**. The buffer currently has a usecnt meaning it is in-use by an in-flight command buffer and can’t be moved.
* **Buffer**. A normal before that is not currently being used and can be moved freely.

Below the pool is a bar representing the “overflow”. These are the buffers that have been paged out of the pool to make space for another buffer. The buffers aren’t paged back in until they are actually used, so having an overflow that grows but then doesn’t shrink again isn’t necessarily a bad thing.

There are some caveats with the simulation that make it not entirely accurate:

* When the kernel driver moves a buffer to compact the pool it does it with a DMA transfer. The simulator doesn’t take into account the fact that the DMA transfer is asynchronous and that buffers to the right of a buffer can’t be moved into a slot that is occupied by an ongoing transfer. This means that if the buffer loses its last usecnt during the transfer of its neighbour, it won’t budge and the actual results will be less compact than shown in the simulation.
* It doesn’t simulate the user-space cache purging. This is a mechanism whereby all of the buffers in Mesa’s user-space cache are marked as purgeable. When the pool is full, it will first remove all of the purgeable buffers before trying to page out any buffers and then Mesa will eventually destroy them. The logging code doesn’t log the purgeable status so the pool will seem more stressed than it should be.
* If user space accesses a buffer mapping that is already in the pool then it won’t be logged as a use. Only the mapping accesses that cause a page fault are logged in the logging output as a use.
