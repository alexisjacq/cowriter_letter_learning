#!/usr/bin/env python
# coding: utf-8

"""
Nao learning words using the shape_learning package.
This node manages the state machine which maintains the interaction sequence,
receives interaction inputs e.g. which words to write and user demonstrations, 
passes these demonstrations to the learning algorithm, and publishes the 
resulting learned shapes for the robot and tablet to draw.
"""
import numpy
from scipy import interpolate

from letter_learning_interaction.interaction_settings import InteractionSettings

from shape_learning.shape_learner_manager import ShapeLearnerManager
from shape_learning.shape_modeler import ShapeModeler #for normaliseShapeHeight()

from letter_learning_interaction.text_shaper import TextShaper, ScreenManager

import rospy
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped, Point, PointStamped
from std_msgs.msg import String, Empty, Bool, Float64MultiArray, MultiArrayDimension
from letter_learning_interaction.msg import Shape as ShapeMsg

from letter_learning_interaction.state_machine import StateMachine
from copy import deepcopy

rospy.init_node("learning_words_nao")


# -- interaction config parameters come from launch file

#Nao parameters
NAO_IP = rospy.get_param('~nao_ip','127.0.0.1') #default behaviour is to connect to simulator locally
naoSpeaking = rospy.get_param('~nao_speaking',True) #whether or not the robot should speak
naoWriting = rospy.get_param('~nao_writing',True) #whether or not the robot should move its arms
naoStanding = rospy.get_param('~nao_standing', True) #whether or not the robot should stand or rest on its knies 
naoConnected = rospy.get_param('~use_robot_in_interaction',True) #whether or not the robot is being used for the interaction (looking, etc.)
naoWriting = naoWriting and naoConnected #use naoConnected var as the stronger property
naoSpeaking = naoSpeaking and naoConnected

LANGUAGE = rospy.get_param('~language','english')

FRONT_INTERACTION = True

NAO_HANDEDNESS = rospy.get_param('~nao_handedness','right')

if NAO_HANDEDNESS.lower()=='right':
    effector = "RArm"
elif NAO_HANDEDNESS.lower()=='left':
    effector = "LArm"
else: 
    print ('error in handedness param')


#shape params       
FRAME = rospy.get_param('~writing_surface_frame_id','writing_surface')  #Frame ID to publish points in
FEEDBACK_TOPIC = rospy.get_param('~shape_feedback_topic','shape_feedback') #Name of topic to receive feedback on
SHAPE_TOPIC = rospy.get_param('~trajectory_output_topic','/write_traj') #Name of topic to publish shapes to
BOUNDING_BOXES_TOPIC = rospy.get_param('~bounding_boxes_topic','/boxes_to_draw') #Name of topic to publish bounding boxes of letters to
SHAPE_TOPIC_DOWNSAMPLED = rospy.get_param('~trajectory_output_nao_topic','/write_traj_downsampled') #Name of topic to publish shapes to

SHAPE_LOGGING_PATH = rospy.get_param('~shape_log','') # path to a log file where all learning steps will be stored

#tablet params        
CLEAR_SURFACE_TOPIC = rospy.get_param('~clear_writing_surface_topic','clear_screen')
SHAPE_FINISHED_TOPIC = rospy.get_param('~shape_writing_finished_topic','shape_finished')
#Name of topic to get gestures representing the active shape for demonstration
GESTURE_TOPIC = rospy.get_param('~gesture_info_topic','gesture_info');

#interaction params
WORDS_TOPIC = rospy.get_param('~words_to_write_topic','words_to_write')
PROCESSED_USER_SHAPE_TOPIC = rospy.get_param('~processed_user_shape_topic','user_shapes_processed');#Listen for user shapes
TEST_TOPIC = rospy.get_param('~test_request_topic','test_learning');#Listen for when test card has been shown to the robot
STOP_TOPIC = rospy.get_param('~stop_request_topic','stop_learning');#Listen for when stop card has been shown to the robot
NEW_CHILD_TOPIC = rospy.get_param('~new_teacher_topic','new_child');#Welcome a new teacher but don't reset learning algorithm's 'memory'
personSide = rospy.get_param('~person_side', NAO_HANDEDNESS.lower()) #side where person is (left/right)
PUBLISH_STATUS_TOPIC = rospy.get_param('~camera_publishing_status_topic','camera_publishing_status') #Controls the camera based on the interaction state (turn it off for writing b/c CPU gets maxed)

alternateSidesLookingAt = False #if true, nao will look to a different side each time. (not super tested)
global nextSideToLookAt
nextSideToLookAt = 'Right'


# -- technical parameters come from the interaction_settings module

#initialise arrays of phrases to say at relevant times
introPhrase, demo_response_phrases, asking_phrases_after_feedback, asking_phrases_after_word, word_response_phrases, word_again_response_phrases, testPhrase, thankYouPhrase = InteractionSettings.getPhrases(LANGUAGE)

demo_response_phrases_counter = 0
asking_phrases_after_feedback_counter = 0
asking_phrases_after_word_counter = 0
word_response_phrases_counter = 0
word_again_response_phrases_counter = 0

#get appropriate angles for looking at things
headAngles_lookAtTablet_down, headAngles_lookAtTablet_right, headAngles_lookAtTablet_left, headAngles_lookAtPerson_front, headAngles_lookAtPerson_right, headAngles_lookAtPerson_left = InteractionSettings.getHeadAngles()

#trajectory publishing parameters
t0, dt, delayBeforeExecuting = InteractionSettings.getTrajectoryTimings(naoWriting)
NUMDESIREDSHAPEPOINTS = 7.0;#Number of points to downsample the length of shapes to 
NUMPOINTS_SHAPEMODELER = 70 #Number of points used by ShapeModelers (@todo this could vary for each letter)
DOWNSAMPLEFACTOR = float(NUMPOINTS_SHAPEMODELER-1)/float(NUMDESIREDSHAPEPOINTS-1)

drawingLetterSubstates = ['WAITING_FOR_ROBOT_TO_CONNECT', 'WAITING_FOR_TABLET_TO_CONNECT', 'PUBLISHING_LETTER']

pub_camera_status = rospy.Publisher(PUBLISH_STATUS_TOPIC,Bool, queue_size=10)
pub_traj = rospy.Publisher(SHAPE_TOPIC, Path, queue_size=10)
pub_bounding_boxes = rospy.Publisher(BOUNDING_BOXES_TOPIC, Float64MultiArray, queue_size=10)
pub_traj_downsampled = rospy.Publisher(SHAPE_TOPIC_DOWNSAMPLED, Path, queue_size=10)
pub_clear = rospy.Publisher(CLEAR_SURFACE_TOPIC, Empty, queue_size=10)



# ---------------------------------------- CALLBACK METHODS FOR ROS SUBSCRIBERS

activeLetter = None
demoShapesReceived = []
def onUserDrawnShapeReceived(shape):
    """
    The main task here is to identify the letter(s) we got demos for
    """
    global demoShapesReceived
    global activeLetter

    if(stateMachine.get_state() == "WAITING_FOR_FEEDBACK"
       or stateMachine.get_state() == "ASKING_FOR_FEEDBACK"):

        nbpts = len(shape.path)/2
        path = zip(shape.path[:nbpts], [-y for y in shape.path[nbpts:]])
        demo_from_template = screenManager.split_path_from_template(path)
        if demo_from_template:
            rospy.loginfo('Received template demonstration for letters ' + str(demo_from_template.keys()))

            for name, path in demo_from_template.items():

                flatpath = [x for x, y in path]
                flatpath.extend([-y for x, y in path])

                demoShapesReceived.append(ShapeMsg(path=flatpath, shapeType=name))

        else:

            if activeLetter:
                shape.shapeType = activeLetter
                activeLetter = None
                rospy.loginfo('Received demonstration for selected letter ' + shape.shapeType)
            else:
                letter, bb = screenManager.find_letter(shape.path)

                if letter:
                    shape.shapeType = letter
                    #pub_bounding_boxes.publish(make_bounding_box_msg(bb, selected=True))
                    rospy.loginfo('Received demonstration for ' + shape.shapeType)
                else:
                    rospy.logwarn('Received demonstration, but unable to find the letter that was demonstrated! Ignoring it.')
                    return

            demoShapesReceived = [shape] #replace any existing feedback with new

    else:
        pass #ignore feedback

shapeFinished = False
def onShapeFinished(message):
    global shapeFinished
    shapeFinished = True #@TODO only register when appropriate

testRequestReceived = False
def onTestRequestReceived(message):
    global testRequestReceived
    #@TODO don't respond to test card unless something has been learnt
    testRequestReceived = True

stopRequestReceived = False
def onStopRequestReceived(message):
    global stopRequestReceived
    stopRequestReceived = True

def onClearScreenReceived(message):
    rospy.loginfo('Clearing display')
    try:
        clear_all_shapes = rospy.ServiceProxy('clear_all_shapes', clearAllShapes)
        resp1 = clear_all_shapes()
    except rospy.ServiceException, e:
        rospy.logerr("Service call failed: %s",e)

wordReceived = None
def onWordReceived(message):
    global wordReceived 
    if(stateMachine.get_state() == "WAITING_FOR_FEEDBACK"
            or stateMachine.get_state() == "WAITING_FOR_WORD"
            or stateMachine.get_state() == "ASKING_FOR_FEEDBACK" 
            or stateMachine.get_state() == "STARTING_INTERACTION"
            or stateMachine.get_state() is None): #state machine hasn't started yet - word probably came from input arguments
        wordReceived = message.data
        rospy.loginfo('Received word: '+wordReceived)
    else:
        wordReceived = None #ignore 

feedbackReceived = None    
def onFeedbackReceived(message):
    global feedbackReceived 
    if(stateMachine.get_state() == "ASKING_FOR_FEEDBACK" 
            or stateMachine.get_state() == "WAITING_FOR_FEEDBACK" 
            or stateMachine.get_state() == "WAITING_FOR_LETTER_TO_FINISH" ):
        feedbackReceived = message #replace any existing feedback with new
        rospy.loginfo('Received feedback')
    elif stateMachine.get_state() == "RESPONDING_TO_FEEDBACK":
        feedbackReceived = None #ignore feedback

def onNewChildReceived(message):
    global nextSideToLookAt
    if naoWriting:
        if naoStanding:
            postureProxy.goToPosture("StandInit", 0.3)
        else:
            motionProxy.rest()
            motionProxy.setStiffnesses(["Head", "LArm", "RArm"], 0.5)
            motionProxy.setStiffnesses(["LHipYawPitch", "LHipRoll", "LHipPitch", "RHipYawPitch", "RHipRoll", "RHipPitch"], 0.8)

    if naoSpeaking:
        if alternateSidesLookingAt:
            lookAndAskForFeedback(introPhrase,nextSideToLookAt)
        else:
            lookAndAskForFeedback(introPhrase,personSide)
    #clear screen
    pub_clear.publish(Empty())
    rospy.sleep(0.5)

def onSetActiveShapeGesture(message):
    global activeLetter

    activeLetter, bb = screenManager.closest_letter(message.point.x, message.point.y, strict=True)
    
    #if activeLetter:
    #    pub_bounding_boxes.publish(make_bounding_box_msg(bb, selected=True))

# ------------------------------- METHODS FOR DIFFERENT STATES IN STATE MACHINE

def respondToDemonstration(infoFromPrevState):
    #print('------------------------------------------ RESPONDING_TO_DEMONSTRATION')
    rospy.loginfo("STATE: RESPONDING_TO_DEMONSTRATION")
    demoShapesReceived = infoFromPrevState['demoShapesReceived']

    # update the shape models with the incoming demos
    new_shapes = []

    letters = "".join([s.shapeType for s in demoShapesReceived])
    
    if naoSpeaking:
        global demo_response_phrases_counter
        try:
            toSay = demo_response_phrases[demo_response_phrases_counter] % letters
        except TypeError: #string wasn't meant to be formatted
            toSay = demo_response_phrases[demo_response_phrases_counter]
        demo_response_phrases_counter += 1
        if demo_response_phrases_counter==len(demo_response_phrases):
            demo_response_phrases_counter = 0
        textToSpeech.say(toSay)
        rospy.loginfo('NAO: '+toSay)


    for shape in demoShapesReceived:
        glyph = shape.path
        shapeName = shape.shapeType

        glyph = downsampleShape(glyph)


        rospy.loginfo("Received demo for " + shapeName)
        shapeIndex = wordManager.currentCollection.index(shapeName)
        shape = wordManager.respondToDemonstration(shapeIndex, glyph)

        new_shapes.append(shape)

    state_goTo = deepcopy(drawingLetterSubstates)
    nextState = state_goTo.pop(0)
    infoForNextState = {'state_goTo': state_goTo, 'state_cameFrom': "RESPONDING_TO_DEMONSTRATION",'shapesToPublish': new_shapes}
    return nextState, infoForNextState

def respondToDemonstrationWithFullWord(infoFromPrevState):
    #print('------------------------------------------ RESPONDING_TO_DEMONSTRATION_FULL_WORD')
    rospy.loginfo("STATE: RESPONDING_TO_DEMONSTRATION_FULL_WORD")
    demoShapesReceived = infoFromPrevState['demoShapesReceived']

    letters = "".join([s.shapeType for s in demoShapesReceived])
    
    if naoSpeaking:
        global demo_response_phrases_counter
        try:
            toSay = demo_response_phrases[demo_response_phrases_counter] % letters
        except TypeError: #string wasn't meant to be formatted
            toSay = demo_response_phrases[demo_response_phrases_counter]
        demo_response_phrases_counter += 1
        if demo_response_phrases_counter==len(demo_response_phrases):
            demo_response_phrases_counter = 0
        textToSpeech.say(toSay)
        rospy.loginfo('NAO: '+toSay)


    # 1- update the shape models with the incoming demos
    for shape in demoShapesReceived:
        glyph = shape.path
        shapeName = shape.shapeType

        glyph = downsampleShape(glyph)
        rospy.loginfo("Received demo for " + shapeName)
        shapeIndex = wordManager.currentCollection.index(shapeName)
        wordManager.respondToDemonstration(shapeIndex, glyph)

    # 2- display the update word

    #clear screen
    screenManager.clear()
    pub_clear.publish(Empty())
    rospy.sleep(0.5)

    shapesToPublish = wordManager.shapesOfCurrentCollection()

    nextState = 'PUBLISHING_WORD'
    infoForNextState = {'state_cameFrom': "RESPONDING_TO_DEMONSTRATION_FULL_WORD",
                        'shapesToPublish': shapesToPublish,
                        'wordToWrite': wordManager.currentCollection}

    return nextState, infoForNextState


def publishShape(infoFromPrevState):
    # TODO: publishShape is currently broken. Needs to be updated to use the
    # TextShaper and the ScreenManager, like publishWord
    raise RuntimeError("publish shape is currently broken!!")

    #print('------------------------------------------ PUBLISHING_LETTER')
    rospy.loginfo("STATE: PUBLISHING_LETTER")
    shapesToPublish = infoFromPrevState['shapesToPublish']
    shape = shapesToPublish.pop(0) #publish next remaining shape (and remove from list)

    try:
        display_new_shape = rospy.ServiceProxy('display_new_shape', displayNewShape)
        response = display_new_shape(shape_type_code = shape.shapeType_code)
        shapeCentre = numpy.array([response.location.x, response.location.y])
    except rospy.ServiceException, e:
        print "Service call failed: %s"%e

    headerString = shape.shapeType+'_'+str(shape.paramsToVary)+'_'+str(shape.paramValues)

    traj_downsampled = make_traj_msg(shape.path, shapeCentre, headerString, t0, True, dt) #for robot

    traj = make_traj_msg(shape.path, shapeCentre, headerString, t0, False, float(dt)/DOWNSAMPLEFACTOR)

    trajStartPosition = traj.poses[0].pose.position

    if naoConnected:
        lookAtTablet()

    pub_traj_downsampled.publish(traj_downsampled)
    pub_traj.publish(traj)


    nextState = "WAITING_FOR_LETTER_TO_FINISH"
    infoForNextState = {'state_cameFrom':  "PUBLISHING_LETTER",'state_goTo': ["ASKING_FOR_FEEDBACK"],'centre': trajStartPosition,'shapePublished':shape.shapeType} #only appends most recent shape's info (@TODO)
    if(len(shapesToPublish) > 0): #more shapes to publish
        state_goTo = deepcopy(drawingLetterSubstates);#come back to publish the remaining shapes
        infoForNextState = {'state_goTo': state_goTo,'state_cameFrom': "PUBLISHING_LETTER",'shapesToPublish': shapesToPublish,'centre': trajStartPosition}

    return nextState, infoForNextState

def publishWord(infoFromPrevState):
    #print('------------------------------------------ PUBLISHING_WORD')
    rospy.loginfo("STATE: PUBLISHING_WORD")

    shapedWord = textShaper.shapeWord(wordManager)
    placedWord = screenManager.place_word(shapedWord)

    traj = make_traj_msg(placedWord, float(dt)/DOWNSAMPLEFACTOR)

    # downsampled the trajectory for the robot arm motion
    downsampledShapedWord = deepcopy(placedWord)
    downsampledShapedWord.downsample(DOWNSAMPLEFACTOR)

    downsampledTraj = make_traj_msg(downsampledShapedWord, dt)

    ###
    # Request the tablet to display the letters' and word's bounding boxes
    #for bb in placedWord.get_letters_bounding_boxes():
    #    pub_bounding_boxes.publish(make_bounding_box_msg(bb, selected=False))
    #    rospy.sleep(0.1) #leave some time for the tablet to process the bbs

    #pub_bounding_boxes.publish(make_bounding_box_msg(placedWord.get_global_bb(), selected=False))
    ###

    trajStartPosition = traj.poses[0].pose.position

    if naoConnected:
        lookAtTablet()

    pub_traj_downsampled.publish(downsampledTraj)
    pub_traj.publish(traj)

    nextState = "WAITING_FOR_LETTER_TO_FINISH"
    infoForNextState = {'state_cameFrom':  "PUBLISHING_WORD",'state_goTo': ["ASKING_FOR_FEEDBACK"],'centre': trajStartPosition, 'wordWritten':infoFromPrevState['wordToWrite']}

    return nextState, infoForNextState


infoToRestore_waitForShapeToFinish = rospy.Subscriber(SHAPE_FINISHED_TOPIC, String, onShapeFinished)
global shape_finished_subscriber
def waitForShapeToFinish(infoFromPrevState):
    global infoToRestore_waitForShapeToFinish
    #FORWARDER STATE

    #first time into this state preparations
    if infoFromPrevState['state_cameFrom'] != "WAITING_FOR_LETTER_TO_FINISH":
        #print('------------------------------------------ WAITING_FOR_LETTER_TO_FINISH')
        rospy.loginfo("STATE: WAITING_FOR_LETTER_TO_FINISH")
        infoToRestore_waitForShapeToFinish = infoFromPrevState

    infoForNextState = {'state_cameFrom': 'WAITING_FOR_LETTER_TO_FINISH'}
    nextState = None

    #once shape has finished
    global shapeFinished
    if shapeFinished:
        
        # draw the templates for the demonstrations
        ref_boundingboxes = screenManager.place_reference_boundingboxes(wordManager.currentCollection)
        for bb in ref_boundingboxes:
            pub_bounding_boxes.publish(make_bounding_box_msg(bb, selected=False))
            rospy.sleep(0.2) #leave some time for the tablet to process the bbs



        shapeFinished = False

        infoForNextState = infoToRestore_waitForShapeToFinish
        try:
            if infoForNextState['state_goTo'] is not None and len(infoForNextState['state_goTo'])>0:
                nextState = infoForNextState['state_goTo'].pop(0) #go to the next state requested to and remove it from the list
                #@TODO make sure it actually gets executed before popping it...
        except:
            #nothing planned..
            nextState = 'WAITING_FOR_FEEDBACK'
    '''       
    #act if the tablet disconnects
    if not tabletWatchdog.isResponsive():
        nextState = 'WAITING_FOR_TABLET_TO_CONNECT'
        infoForNextState = {'state_goTo': ['WAITING_FOR_FEEDBACK'], 'state_cameFrom': 'WAITING_FOR_LETTER_TO_FINISH'}
        #@TODO go back and re-send whatever we just send that we never got the shapeFinished message for...
    '''

    if stopRequestReceived:
        nextState = "STOPPING"  

    if nextState is None:
        #default behaviour is to keep waiting
        rospy.sleep(0.1) #don't check straight away
        nextState = 'WAITING_FOR_LETTER_TO_FINISH'
        infoForNextState = {'state_cameFrom': 'WAITING_FOR_LETTER_TO_FINISH'}

    return nextState, infoForNextState

#NOTE THAT THIS WAS FOR TOUCH-BASED FEEDBACK, WHICH ISN'T USED ANYMORE
def respondToFeedback(infoFromPrevState):
    #print('------------------------------------------ RESPONDING_TO_FEEDBACK')
    rospy.loginfo("STATE: RESPONDING_TO_FEEDBACK")
    global shapeFinished #@TODO: make class attribute

    stringReceived = infoFromPrevState['feedbackReceived']

    nextState = "WAITING_FOR_FEEDBACK"
    infoForNextState = {'state_cameFrom': "ASKING_FOR_FEEDBACK"}

    #convert feedback string into settings
    feedback = stringReceived.data.split('_')
    processMessage = True
    try:
        shapeIndex_messageFor = int(feedback[0])
    except:
        rospy.logerr('Shape type index must be an integer. Received ' + feedback[0])
        processMessage = False

    try:
        bestShape_index = int(feedback[1])
    except:
        rospy.logerr('Best shape index must be an integer. Received ' + feedback[0])
        processMessage = False

    noNewShape = False #usually make a new shape based on feedback
    if(len(feedback)>2): 
        feedbackMessage = feedback[2]
        if feedbackMessage == 'noNewShape':
            noNewShape = True
        else:
            processMessage = False
            rospy.logerr('Unknown message received in feedback string: '+feedbackMessage)  

    if(processMessage):    
        if(noNewShape): #just respond to feedback, don't make new shape 
            if naoSpeaking:
                toSay = 'Ok, thanks for helping me'
                rospy.loginfo('NAO: '+toSay)
                textToSpeech.say(toSay)
            #pass feedback to shape manager
            response = wordManager.feedbackManager(shapeIndex_messageFor, bestShape_index, noNewShape)
            if response == -1:
                rospy.logerr('Something\'s gone wrong in the feedback manager')

        else:
            if naoSpeaking:
                shape_messageFor = wordManager.shapeAtIndexInCurrentCollection(shapeIndex_messageFor)
                toSay = 'Ok, I\'ll work on the '+shape_messageFor
                rospy.loginfo('NAO: '+toSay)
                textToSpeech.say(toSay)

            [numItersConverged, newShape] = wordManager.feedbackManager(shapeIndex_messageFor, bestShape_index, noNewShape)

            if numItersConverged == 0:
                state_goTo = deepcopy(drawingLetterSubstates)
                nextState = state_goTo.pop(0)
                infoForNextState = {'state_goTo': state_goTo,'state_cameFrom': "RESPONDING_TO_FEEDBACK",'shapesToPublish': [newShape]}
            else:
                pass #@TODO handle convergence

    global wordReceived
    if wordReceived is not None:
        infoForNextState['wordReceived'] = wordReceived
        wordReceived = None
        nextState = "RESPONDING_TO_NEW_WORD"
    global testRequestReceived
    if testRequestReceived:
        testRequestReceived = None
        nextState = "RESPONDING_TO_TEST_CARD"
    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState


def respondToNewWord(infoFromPrevState):
    #print('------------------------------------------ RESPONDING_TO_NEW_WORD')
    rospy.loginfo("STATE: RESPONDING_TO_NEW_WORD")
    global shapeFinished, wordManager #@TODO make class attribute 
    wordToLearn = infoFromPrevState['wordReceived']
    wordSeenBefore = wordManager.newCollection(wordToLearn)
    if naoSpeaking:
        if wordSeenBefore:
            global word_again_response_phrases_counter
            try:
                toSay = word_again_response_phrases[word_again_response_phrases_counter]%wordToLearn
            except TypeError: #string wasn't meant to be formatted
                toSay = word_again_response_phrases[word_again_response_phrases_counter]
            word_again_response_phrases_counter += 1
            if word_again_response_phrases_counter==len(word_again_response_phrases):
                word_again_response_phrases_counter = 0

        else:
            global word_response_phrases_counter
            try:
                toSay = word_response_phrases[word_response_phrases_counter]%wordToLearn
            except TypeError: #string wasn't meant to be formatted
                toSay = word_response_phrases[word_response_phrases_counter]
            word_response_phrases_counter += 1
            if word_response_phrases_counter==len(word_response_phrases):
                word_response_phrases_counter = 0

        rospy.loginfo('NAO: '+toSay)
        textToSpeech.say(toSay)   

    #clear screen
    screenManager.clear()
    pub_clear.publish(Empty())
    rospy.sleep(0.5)

    #start learning    
    shapesToPublish = []   
    for i in range(len(wordToLearn)):
        shape = wordManager.startNextShapeLearner()
        shapesToPublish.append(shape)

    nextState = 'PUBLISHING_WORD'
    infoForNextState = {'state_cameFrom': "RESPONDING_TO_NEW_WORD",'shapesToPublish': shapesToPublish,'wordToWrite': wordToLearn}

    global wordReceived
    if wordReceived is not None:
        infoForNextState['wordReceived'] = wordReceived
        wordReceived = None
        nextState = "RESPONDING_TO_NEW_WORD"
    global testRequestReceived
    if testRequestReceived:
        testRequestReceived = None
        nextState = "RESPONDING_TO_TEST_CARD"
    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState


def askForFeedback(infoFromPrevState): 
    global nextSideToLookAt
    #print('------------------------------------------ ASKING_FOR_FEEDBACK')
    rospy.loginfo("STATE: ASKING_FOR_FEEDBACK")
    centre = infoFromPrevState['centre']
    rospy.loginfo(infoFromPrevState['state_cameFrom'])
    if infoFromPrevState['state_cameFrom'] == "PUBLISHING_WORD":
        wordWritten = infoFromPrevState['wordWritten']
        rospy.loginfo('Asking for feedback on word '+wordWritten)
        if naoSpeaking:
            global asking_phrases_after_word_counter
            try:
                toSay = asking_phrases_after_word[asking_phrases_after_word_counter]%wordWritten
            except TypeError: #string wasn't meant to be formatted
                toSay = asking_phrases_after_word[asking_phrases_after_word_counter]
            asking_phrases_after_word_counter += 1
            if asking_phrases_after_word_counter==len(asking_phrases_after_word):
                asking_phrases_after_word_counter = 0

            if(alternateSidesLookingAt):  
                lookAndAskForFeedback(toSay,nextSideToLookAt)
                if nextSideToLookAt == 'Left':
                    nextSideToLookAt = 'Right'
                else:
                    nextSideToLookAt = 'Left'
            else:
                lookAndAskForFeedback(toSay,personSide)

            lookAtTablet()
    elif infoFromPrevState['state_cameFrom'] == "PUBLISHING_LETTER":
        shapeType = infoFromPrevState['shapePublished']
        rospy.loginfo('Asking for feedback on letter '+shapeType)
        if naoSpeaking:
            global asking_phrases_after_feedback_counter
            try:
                toSay = asking_phrases_after_feedback[asking_phrases_after_feedback_counter]%shapeType
            except TypeError: #string wasn't meant to be formatted
                toSay = asking_phrases_after_feedback[asking_phrases_after_feedback_counter]
            asking_phrases_after_feedback_counter += 1
            if asking_phrases_after_feedback_counter==len(asking_phrases_after_feedback):
                asking_phrases_after_feedback_counter = 0

            if(alternateSidesLookingAt):  
                lookAndAskForFeedback(toSay,nextSideToLookAt)
                if nextSideToLookAt == 'Left':
                    nextSideToLookAt = 'Right'
                else:
                    nextSideToLookAt = 'Left'
            else:
                lookAndAskForFeedback(toSay,personSide)

            lookAtTablet()
    '''
    #this doesn't get entered into anymore
    elif infoFromPrevState['state_cameFrom'] == "RESPONDING_TO_DEMONSTRATION":
        rospy.loginfo('Asking for feedback on demo response...')
        if naoSpeaking:
            lookAndAskForFeedback("How about now?")
            lookAtTablet()
    '''
    nextState = "WAITING_FOR_FEEDBACK"
    infoForNextState = {'state_cameFrom': "ASKING_FOR_FEEDBACK"}
    global wordReceived
    if wordReceived is not None:
        infoForNextState['wordReceived'] = wordReceived
        wordReceived = None
        nextState = "RESPONDING_TO_NEW_WORD"
    global testRequestReceived
    if wordReceived is not None:
        testRequestReceived = None
        nextState = "RESPONDING_TO_TEST_CARD"
    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState


def respondToTestCard(infoFromPrevState):
    #print('------------------------------------------ RESPONDING_TO_TEST_CARD')
    rospy.loginfo("STATE: RESPONDING_TO_TEST_CARD")
    if naoSpeaking:
        textToSpeech.say(testPhrase)
        rospy.loginfo("NAO: "+testPhrase)
    nextState = "WAITING_FOR_WORD"
    infoForNextState = {'state_cameFrom': "RESPONDING_TO_TEST_CARD"}
    return nextState, infoForNextState


def stopInteraction(infoFromPrevState):
    #print('------------------------------------------ STOPPING')
    rospy.loginfo("STATE: STOPPING")
    if naoSpeaking:
        textToSpeech.say(thankYouPhrase)  
    if naoConnected:
        motionProxy.wbEnableEffectorControl(effector,False)
        motionProxy.rest()
    nextState = "EXIT"
    infoForNextState = 0
    rospy.signal_shutdown('Interaction exited')
    return nextState, infoForNextState


def startInteraction(infoFromPrevState):
    global nextSideToLookAt
    #print('------------------------------------------ STARTING_INTERACTION')
    rospy.loginfo("STATE: STARTING_INTERACTION")
    if naoSpeaking:
        if(alternateSidesLookingAt): 
            lookAndAskForFeedback(introPhrase,nextSideToLookAt)
        else:
            lookAndAskForFeedback(introPhrase,personSide)

    nextState = "WAITING_FOR_WORD"
    infoForNextState = {'state_cameFrom': "STARTING_INTERACTION"}
    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState


def waitForWord(infoFromPrevState):
    global wordReceived

    if infoFromPrevState['state_cameFrom'] != "WAITING_FOR_WORD":
        #print('------------------------------------------ WAITING_FOR_WORD')
        rospy.loginfo("STATE: WAITING_FOR_WORD")
        pub_camera_status.publish(True) #turn camera on
    if infoFromPrevState['state_cameFrom'] == "STARTING_INTERACTION":
        pass

    infoForNextState = {'state_cameFrom': "WAITING_FOR_WORD"}
    if wordReceived is None:
        nextState = "WAITING_FOR_WORD"
        rospy.sleep(0.1) #don't check again immediately
    else:
        infoForNextState['wordReceived'] = wordReceived
        wordReceived = None
        nextState = "RESPONDING_TO_NEW_WORD"
        pub_camera_status.publish(False) #turn camera off
    if stopRequestReceived:
        nextState = "STOPPING"
        pub_camera_status.publish(False) #turn camera off
    return nextState, infoForNextState


def waitForFeedback(infoFromPrevState):

    if infoFromPrevState['state_cameFrom'] != "WAITING_FOR_FEEDBACK":
        #print('------------------------------------------ WAITING_FOR_FEEDBACK')
        rospy.loginfo("STATE: WAITING_FOR_FEEDBACK")
        pub_camera_status.publish(True) #turn camera on

    infoForNextState = {'state_cameFrom': "WAITING_FOR_FEEDBACK"}
    nextState = None

    global feedbackReceived    
    if feedbackReceived is not None:
        infoForNextState['feedbackReceived'] = feedbackReceived
        feedbackReceived = None
        nextState = "RESPONDING_TO_FEEDBACK"
        infoForNextState['state_goTo'] = [nextState]
        nextState = 'WAITING_FOR_ROBOT_TO_CONNECT'

    global demoShapesReceived    
    if demoShapesReceived:
        infoForNextState ['demoShapesReceived'] = demoShapesReceived
        demoShapesReceived = []
        nextState = "RESPONDING_TO_DEMONSTRATION_FULL_WORD"   
        infoForNextState['state_goTo'] = [nextState] #ensure robot is connected before going to that state
        nextState = 'WAITING_FOR_ROBOT_TO_CONNECT'

    global wordReceived
    if wordReceived is not None:
        infoForNextState['wordReceived'] = wordReceived
        wordReceived = None
        nextState = "RESPONDING_TO_NEW_WORD"
        infoForNextState['state_goTo'] = [nextState] #ensure robot is connected before going to that state
        nextState = 'WAITING_FOR_ROBOT_TO_CONNECT'

    global testRequestReceived
    if testRequestReceived:
        testRequestReceived = None
        nextState = "RESPONDING_TO_TEST_CARD"
        infoForNextState['state_goTo'] = [nextState] #ensure robot is connected before going to that state
        nextState = 'WAITING_FOR_ROBOT_TO_CONNECT'

    if stopRequestReceived:
        nextState = "STOPPING"

    if nextState != 'WAITING_FOR_FEEDBACK':
        pub_camera_status.publish(False) #turn camera off

    if nextState is None:
        #default behaviour is to loop
        rospy.sleep(0.1) #don't check again immediately
        nextState = "WAITING_FOR_FEEDBACK"
        infoForNextState = {'state_cameFrom': "WAITING_FOR_FEEDBACK"}

    return nextState, infoForNextState    


#def respondToTabletDisconnect(infoFromPrevState):
 #   infoForNextState = {'state_toReturnTo': "PUBLISHING_LETTER"}


infoToRestore_waitForRobotToConnect = None
def waitForRobotToConnect(infoFromPrevState):
    global infoToRestore_waitForRobotToConnect
    #FORWARDER STATE
    if infoFromPrevState['state_cameFrom'] != "WAITING_FOR_ROBOT_TO_CONNECT":
        #print('------------------------------------------ waiting_for_robot_to_connect')
        rospy.loginfo("STATE: waiting_for_robot_to_connect")
        infoToRestore_waitForRobotToConnect = infoFromPrevState

    nextState = "WAITING_FOR_ROBOT_TO_CONNECT"
    infoForNextState = {'state_cameFrom': "WAITING_FOR_ROBOT_TO_CONNECT"}

    #if robotWatchdog.isResponsive() or not naoConnected:
    if(True): #don't use watchdog for now
        infoForNextState = infoToRestore_waitForRobotToConnect
        nextState = infoForNextState['state_goTo'].pop(0)
    else:
        rospy.sleep(0.1) #don't check again immediately

    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState


infoToRestore_waitForTabletToConnect = None
def waitForTabletToConnect(infoFromPrevState):
    global infoToRestore_waitForTabletToConnect
    #FORWARDER STATE
    if infoFromPrevState['state_cameFrom'] != "WAITING_FOR_TABLET_TO_CONNECT":
        #print('------------------------------------------ waiting_for_tablet_to_connect')
        rospy.loginfo("STATE: waiting_for_tablet_to_connect")
        infoToRestore_waitForTabletToConnect = infoFromPrevState

    nextState = "WAITING_FOR_TABLET_TO_CONNECT"
    infoForNextState = {'state_cameFrom': "WAITING_FOR_TABLET_TO_CONNECT"}

    if(tabletWatchdog.isResponsive()): #reconnection - send message to wherever it was going
        infoForNextState = infoToRestore_waitForTabletToConnect
        nextState = infoForNextState['state_goTo'].pop(0)
    else:
        rospy.sleep(0.1) #don't check again immediately

    if stopRequestReceived:
        nextState = "STOPPING"
    return nextState, infoForNextState    


# -------------------------------------------------------------- HELPER METHODS

def downsampleShape(shape):
    #downsample user-drawn shape so appropriate size for shapeLearner
    numPointsInShape = len(shape)/2
    x_shape = shape[0:numPointsInShape]
    y_shape = shape[numPointsInShape:]

    if isinstance(x_shape,numpy.ndarray): #convert arrays to lists for interp1d
        x_shape = (x_shape.T).tolist()[0]
        y_shape = (y_shape.T).tolist()[0]

    #make shape have the same number of points as the shape_modeler
    t_current = numpy.linspace(0, 1, numPointsInShape)
    t_desired = numpy.linspace(0, 1, NUMPOINTS_SHAPEMODELER)
    f = interpolate.interp1d(t_current, x_shape, kind='cubic')
    x_shape = f(t_desired)
    f = interpolate.interp1d(t_current, y_shape, kind='cubic')
    y_shape = f(t_desired)

    shape = []
    shape[0:NUMPOINTS_SHAPEMODELER] = x_shape
    shape[NUMPOINTS_SHAPEMODELER:] = y_shape

    shape = ShapeModeler.normaliseShapeHeight(numpy.array(shape))
    shape = numpy.reshape(shape, (-1, 1)) #explicitly make it 2D array with only one column

    return shape

def make_bounding_box_msg(bbox, selected=False):

    bb = Float64MultiArray()
    bb.layout.data_offset = 0
    dim = MultiArrayDimension()
    dim.label = "bb" if not selected else "select" # we use the label of the first dimension to carry the selected/not selected infomation
    bb.layout.dim = [dim]
    
    x_min, y_min, x_max, y_max = bbox
    bb.data = [x_min, y_min, x_max, y_max]

    return bb

def make_traj_msg(shapedWord, deltaT):

    traj = Path()
    traj.header.frame_id = FRAME
    traj.header.stamp = rospy.Time.now() + rospy.Duration(delayBeforeExecuting)

    pointIdx = 0
    for path in shapedWord.get_letters_paths():
        for x, y in path:
            point = PoseStamped()

            point.pose.position.x = x
            point.pose.position.y = y
            point.header.frame_id = FRAME
            point.header.stamp = rospy.Time(t0 + pointIdx * deltaT) #@TODO allow for variable time between points for now

            traj.poses.append(deepcopy(point))

            pointIdx += 1

    return traj

def lookAtTablet():
    if FRONT_INTERACTION:
        motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtTablet_down,0.2)

    else:
        if(effector=="RArm"):   #tablet will be on our right
            motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtTablet_right,0.2)
        else: 
            motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtTablet_left,0.2)

def lookAndAskForFeedback(toSay,side):
    if naoWriting:
        #put arm down
        motionProxy.angleInterpolationWithSpeed(effector,armJoints_standInit, 0.3)

    if FRONT_INTERACTION:
        motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtPerson_front,0.2)
    else:
        if(side=="Right"):   #person will be on our right
            motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtPerson_right,0.2)
        else:                   #person will be on our left
            motionProxy.setAngles(["HeadYaw", "HeadPitch"],headAngles_lookAtPerson_left,0.2)

    if naoSpeaking:
        textToSpeech.say(toSay)
        rospy.loginfo('NAO: '+toSay)


### --------------------------------------------------------------- MAIN
shapesLearnt = []
wordsLearnt = []
shapeLearners = []
currentWord = []
settings_shapeLearners = []


if __name__ == "__main__":

    datasetDirectory = rospy.get_param('~dataset_directory','default')
    if(datasetDirectory.lower()=='default'): #use default
        import inspect
        fileName = inspect.getsourcefile(ShapeModeler)
        installDirectory = fileName.split('/lib')[0]
        datasetDirectory = installDirectory + '/share/shape_learning/letter_model_datasets/uji_pen_chars2'
    '''
    #@TODO reenable command line usage
    #parse arguments
    import argparse
    parser = argparse.ArgumentParser(description='Publish shapes on the \
            /shapes_to_draw topic and adapt them based on feedback received on the /shape_feedback topic')
    parser.add_argument('word', nargs='?', action="store",
                    help='a string containing the letters to be learnt (if not present, will wait for one from ROS topic)')
    parser.add_argument('--show', action='store_true', help='display plots of the shapes')

    args = parser.parse_args()

    '''

    stateMachine = StateMachine()
    stateMachine.add_state("STARTING_INTERACTION", startInteraction)
    stateMachine.add_state("WAITING_FOR_ROBOT_TO_CONNECT", waitForRobotToConnect)
    stateMachine.add_state("WAITING_FOR_WORD", waitForWord)
    stateMachine.add_state("RESPONDING_TO_NEW_WORD", respondToNewWord)
    stateMachine.add_state("PUBLISHING_WORD", publishWord)
    stateMachine.add_state("PUBLISHING_LETTER", publishShape)
    stateMachine.add_state("WAITING_FOR_LETTER_TO_FINISH", waitForShapeToFinish)
    stateMachine.add_state("ASKING_FOR_FEEDBACK", askForFeedback)
    stateMachine.add_state("WAITING_FOR_FEEDBACK", waitForFeedback)
    stateMachine.add_state("RESPONDING_TO_FEEDBACK", respondToFeedback)
    stateMachine.add_state("RESPONDING_TO_DEMONSTRATION", respondToDemonstration)
    stateMachine.add_state("RESPONDING_TO_DEMONSTRATION_FULL_WORD", respondToDemonstrationWithFullWord)
    stateMachine.add_state("RESPONDING_TO_TEST_CARD", respondToTestCard)
    #stateMachine.add_state("RESPONDING_TO_TABLET_DISCONNECT", respondToTabletDisconnect)
    stateMachine.add_state("WAITING_FOR_TABLET_TO_CONNECT", waitForTabletToConnect)
    stateMachine.add_state("STOPPING", stopInteraction)
    stateMachine.add_state("EXIT", None, end_state=True)
    stateMachine.set_start("WAITING_FOR_ROBOT_TO_CONNECT")
    infoForStartState = {'state_goTo': ["STARTING_INTERACTION"], 'state_cameFrom': None}

    #listen for a new child signal
    new_child_subscriber = rospy.Subscriber(NEW_CHILD_TOPIC, String, onNewChildReceived)

    #listen for words to write
    words_subscriber = rospy.Subscriber(WORDS_TOPIC, String, onWordReceived)

    #listen for request to clear screen (from tablet)
    clear_subscriber = rospy.Subscriber(CLEAR_SURFACE_TOPIC, Empty, onClearScreenReceived)

    #listen for test time
    test_subscriber = rospy.Subscriber(TEST_TOPIC, Empty, onTestRequestReceived)

    #listen for when to stop
    stop_subscriber = rospy.Subscriber(STOP_TOPIC, Empty, onStopRequestReceived)

    #listen for user-drawn shapes
    shape_subscriber = rospy.Subscriber(PROCESSED_USER_SHAPE_TOPIC, ShapeMsg, onUserDrawnShapeReceived)

    #listen for user-drawn finger gestures
    gesture_subscriber = rospy.Subscriber(GESTURE_TOPIC, PointStamped, onSetActiveShapeGesture); 

    #initialise display manager for shapes (manages positioning of shapes)
    from letter_learning_interaction.srv import *
    rospy.loginfo('Waiting for display manager services to become available')
    rospy.wait_for_service('clear_all_shapes')

    rospy.sleep(2.0)  #Allow some time for the subscribers to do their thing, 
                        #or the first message will be missed (eg. first traj on tablet, first clear request locally)

    from letter_learning_interaction.watchdog import Watchdog #TODO: Make a ROS server so that *everyone* can access the connection statuses
    tabletWatchdog = Watchdog('watchdog_clear/tablet', 0.4)
    #robotWatchdog = Watchdog('watchdog_clear/robot', 0.8)

    rospy.loginfo("Nao configuration: writing=%s, speaking=%s (%s), standing=%s, handedness=%s" % (naoWriting, naoSpeaking, LANGUAGE, naoStanding, NAO_HANDEDNESS))

    if naoConnected:

        from naoqi import ALBroker, ALProxy
        port = 9559
        myBroker = ALBroker("myBroker", #I'm not sure that pyrobots doesn't already have one of these open called NAOqi?
                "0.0.0.0",   # listen to anyone
                0,           # find a free port and use it
                NAO_IP,      # parent broker IP
                port)        # parent broker port
        motionProxy = ALProxy("ALMotion", NAO_IP, port)

        postureProxy = ALProxy("ALRobotPosture", NAO_IP, port)
        textToSpeech = ALProxy("ALTextToSpeech", NAO_IP, port)   
        textToSpeech.setLanguage(LANGUAGE.capitalize())
        #textToSpeech.setVolume(1.0)
        if naoWriting:
            if naoStanding:
                postureProxy.goToPosture("StandInit",0.2)
                motionProxy.wbEnableEffectorControl(effector, True) #turn whole body motion control on
            else:
                motionProxy.rest()
                motionProxy.setStiffnesses(["Head", "LArm", "RArm"], 0.5)
                motionProxy.setStiffnesses(["LHipYawPitch", "LHipRoll", "LHipPitch", "RHipYawPitch", "RHipRoll", "RHipPitch"], 0.8)
                motionProxy.wbEnableEffectorControl(effector, False) #turn whole body motion control off

            armJoints_standInit = motionProxy.getAngles(effector,True)

    #initialise word manager (passes feedback to shape learners and keeps history of words learnt)
    InteractionSettings.setDatasetDirectory(datasetDirectory)
    wordManager = ShapeLearnerManager(InteractionSettings.generateSettings, SHAPE_LOGGING_PATH)
    textShaper = TextShaper()
    screenManager = ScreenManager(0.2, 0.1395)


    '''
    wordToLearn = args.word
    if wordToLearn is not None:
        message = String()
        message.data = wordToLearn
        onWordReceived(message)
    else:
        rospy.loginfo('Waiting for word to write')
    '''
    stateMachine.run(infoForStartState)   

    rospy.spin()

    tabletWatchdog.stop()
    #robotWatchdog.stop()
