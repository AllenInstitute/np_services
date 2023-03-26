# Bensai
## Motivation
Experiments require control and coordination of DAQs, video cameras, stimulus devices - each with their own specific set of function calls. 
- as a result, scripts for running an experiment become highly "coupled" to the device libraries used.

For example, operating a stimulus device typically requires:
- connecting hardware
- setting up the stimulus
- starting 
- monitoring progress
- stopping
- collecting any files generated

with specific code for each task, executed at specific points during an experiment. To switch to a completely different stimulus would likely require modifying code throughout the experiment script - so much that it might be faster to just start-over with a new script based around the new stimulus.

Although swapping out a device might only happen rarely, a high-degree of coupling makes it generally difficult to modify any part of the code without knock-on effects in other parts. Maintainance requires knowledge of the codebase in its entirety. As the experiment becomes more complex (in number of components and tasks), the code becomes more complicated, and we become reluctant to make any changes once it works. The weight of the existing code can completely discourage us from trying new experiments.

<p><a href="https://commons.wikimedia.org/wiki/File:CouplingVsCohesion.svg#/media/File:CouplingVsCohesion.svg"><img src="https://upload.wikimedia.org/wikipedia/commons/0/09/CouplingVsCohesion.svg" alt="CouplingVsCohesion.svg" height="360"></a><br>Fig. 1 - High coupling between modules of code with different responsibilities makes it harder to modify, extend, fix bugs, or hand over to others. <i>Image credit: Евгений Мирошниченко, <a href="http://creativecommons.org/publicdomain/zero/1.0/deed.en" title="Creative Commons Zero, Public Domain Dedication">CC0</a>, <a href="https://commons.wikimedia.org/w/index.php?curid=104043458">Link</a></i></p>

We need to move from b) to a) in the image above: the code for the stimulus device on the right is isolated from the experiment logic on the left; if necessary the module on the right could be swapped out completely without modifying any code on the left. To achieve this, we need to create **a simple, common set of commands for all the components of the experiment**.
The implementation details of each component's tasks need to be moved out of the experiment script and into cohesive, self-contained modules, which can be called through a minimal set of functions: the "interface" connecting the two modules shown in a).

## Aims
The aim of this document is to provide practical advice and guidelines to help simplify the coordination of complex experiments. 

We've tried to create a framework that's as flexible and widely-applicable as possible. In our own experiments, all of the devices and services used could be made to conform to the framework. If any parts become problematic, we'll further refine the design.

Examples are written in Python, and some of the solutions use specific features of the language added in Python 3.8, but the concepts could be implemented in Matlab or any other general-purpose language.

## Verbs/Commands == Tasks == Functions
To create a common set of commands for all components of the experiment, we need to constrain when and in which order they will be executed.

### Core
The following core tasks comprise the life-cycle of a component in an experiment (not all tasks necessarily need to be implemented for every component). 

Each will be implemented as a function that commands the component to carry out any necessary sub-tasks. 

Each function will only be called after the preceding ones have finished.


![Services](./services.drawio.svg)


`initialize()` 
> *Run all setup and configuration to effectively reset the component for fresh use.*

- connect hardware
- start associated computers, software
- set parameters specific to experiment
- clear previously-collected data

***

`test()`
> *Confirm that the component is ready for use and any necessary conditions are met - otherwise raise an error.*

- verify connections, ping hosts
- check available disk space
- check write permissions on filesystem
- verify device is not busy/started

***

`start()`
>*Trigger the component's primary effect.*

- start data acquisition 
- start stimulus presentation
- start video recording
- take a single snapshot

It's desirable to be able to check whether `start()` has already been called on the component. If this information can't be obtained from the device, a flag should be set when `start()` runs. Then, if `start()` is called twice by accident, you can decide whether to stop and restart, raise an error, or do nothing. 

***

`verify()`
> *Assert that the component has started successfully - otherwise raise an error.*

- stimulus is presenting correctly
- data files are increasing in size on disk
 
***

`stop()`
> *Stop the previously-started component.*

- cease acquisition, presentation, etc.
- not implemented for a single snapshot 

***

`finalize()`
> *Handle the result of the most-recent use. Should leave the component ready for additional use.*

- await processing, conversion
- move, rename, backup data

After `finalize()`, the component should be ready for re-use by looping back to `start()` or `initialize()`.

***

`shutdown()`
> *Close the component gracefully.*

- close connections
- close associated software, computers
- switch off power, lights, etc.

***

### Extra

These additional commands don't strictly need to be called within the experiment workflow:

`pretest()`
> *Comprehensively test component functionality by running all other functions.*

- simulate usage in an actual experiment
- generate representative data

Typically, `pretest()` would be run any time changes are made to hardware or software, and before each experiment.

***

`validate()`
> *Assert that the most-recently collected data are as expected.*

- check size of data
- check files can be opened
- check contents  

***

### Rejected
The following functions were tried at one point, but found to be unnecessary.

***

`prime()` 

> *Prime the component for imminent start.*

If priming a device is necessary, it can be handled within `initialize()` or `start()`

***

`configure()`

> *Get and apply configuration to device.*

Configuration is one of the responsbilities of `initialize()` and needn't require it's own command

***

### Zero input arguments 
## Nouns == Devices == Modules or Classes
### Multi-use devices -> multiple nouns
Video camera plus snapshot camera, make two separate classes
## Tips
- configuration (directly modify fields)
- tracking files
- stop on failure
- logging
- timestamps
